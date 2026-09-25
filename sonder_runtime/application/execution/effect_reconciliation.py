"""Bounded startup reconciliation of unresolved worker effects (#515).

A crash can leave admitted intents without a receipt.  Before this module,
those intents stayed fenced until some later caller happened to compose the
same worker binding, and even then only an explicit ``reconcile`` could clear
them.  At runtime composition the host now walks the journal's unresolved
intents in bounded pages and, for every run whose unresolved intents were
admitted by a worker identity this host owns, claims the owner with the
host's epoch and offers each intent to the journal's immutable verifier
registry.

Guarantees:

* Nothing here invokes an effect.  A verifier returns a typed proof from the
  external system of record, which the journal applies in one epoch-checked
  transaction with a durable ``verified:<verifier>:<reference>`` receipt.
  Anything else stays ``uncertain`` and the run stays fenced.
* Bounded: at most ``max_runs`` runs and ``max_pages`` pages of
  ``page_limit`` intents, a wall-clock budget, and the journal's own per-call
  verifier timeout.  Work left over is reported as ``truncated``; the pre-resume
  path reconciles it when that worker is next composed.
* Crash-safe: every step is either read-only or a single journal
  transaction.  Re-running after a crash skips intents that are already
  terminal and retries the rest.
* Foreign runs (an unresolved intent admitted by a worker identity this host
  does not own) are left untouched and reported.
* Live peers: worker identities are per node, so another live runtime
  process on this node composes the same identities.  When the caller's
  ``peer_hosts_live`` probe reports (or cannot rule out) such a peer, the
  whole pass is deferred before any owner is claimed; each worker's own
  pre-restart path still reconciles its runs.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic
from typing import Any

from .effect_journal import EffectIntent, EffectJournalError
from .worker_bindings import (
    AuthenticatedWorkerBinding,
    EffectReconciliationReport,
    ReconciledEffect,
)

_LOG = logging.getLogger(__name__)

DEFAULT_PAGE_LIMIT = 100
DEFAULT_MAX_PAGES = 16
DEFAULT_MAX_RUNS = 64
DEFAULT_TIME_BUDGET_SECONDS = 20.0
DEFAULT_VERIFIER_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True, slots=True)
class StartupReconciliationReport:
    """What one bounded startup pass did.  Content-free by construction."""

    runs: tuple[EffectReconciliationReport, ...] = ()
    foreign_runs: tuple[str, ...] = ()
    failed_runs: tuple[tuple[str, str], ...] = ()
    truncated: bool = False
    # Non-empty when the pass claimed nothing, e.g. "live-peer-host-process".
    deferred: str = ""

    @property
    def resolved(self) -> tuple[ReconciledEffect, ...]:
        return tuple(item for run in self.runs for item in run.resolved)

    @property
    def fenced(self) -> tuple[ReconciledEffect, ...]:
        return tuple(item for run in self.runs for item in run.fenced)

    def summary(self) -> dict[str, object]:
        return {
            "runs": len(self.runs),
            "resolved": len(self.resolved),
            "fenced": len(self.fenced),
            "foreign_runs": len(self.foreign_runs),
            "failed_runs": len(self.failed_runs),
            "truncated": self.truncated,
            "deferred": self.deferred,
        }


def _unresolved_runs(
    journal: Any, *, page_limit: int, max_pages: int,
) -> tuple[dict[str, list[EffectIntent]], bool]:
    """Group unresolved intents by run through bounded keyset pages."""
    page = getattr(journal, "unresolved_page", None)
    if not callable(page):
        raise EffectJournalError("journal cannot enumerate unresolved effects")
    runs: dict[str, list[EffectIntent]] = {}
    after_run, after_sequence = "", 0
    for _ in range(max_pages):
        records, more = page(
            after_run_id=after_run, after_sequence=after_sequence, limit=page_limit,
        )
        for record in records:
            runs.setdefault(record.run_id, []).append(record)
            after_run, after_sequence = record.run_id, record.sequence
        if not more:
            return runs, False
        if not records:
            break
    return runs, True


def reconcile_unresolved_effects(
    journal: Any,
    *,
    owner_epoch: int,
    owns_worker: Callable[[str], bool],
    page_limit: int = DEFAULT_PAGE_LIMIT,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_runs: int = DEFAULT_MAX_RUNS,
    time_budget_seconds: float = DEFAULT_TIME_BUDGET_SECONDS,
    verifier_timeout_seconds: float = DEFAULT_VERIFIER_TIMEOUT_SECONDS,
    emit: Callable[[str, dict[str, object]], None] | None = None,
    peer_hosts_live: Callable[[], bool] | None = None,
) -> StartupReconciliationReport:
    """Reconcile unresolved intents owned by this host, within fixed bounds."""
    if type(owner_epoch) is not int or owner_epoch < 1:
        raise ValueError("owner_epoch must be positive")
    if not callable(owns_worker):
        raise TypeError("owns_worker must be callable")
    for name, value, ceiling in (
        ("page_limit", page_limit, 10_000), ("max_pages", max_pages, 10_000),
        ("max_runs", max_runs, 10_000),
    ):
        if type(value) is not int or not 1 <= value <= ceiling:
            raise ValueError(f"{name} must be within 1..{ceiling}")
    if not 0 < time_budget_seconds <= 600:
        raise ValueError("time_budget_seconds must be within 0..600")
    deadline = monotonic() + time_budget_seconds
    grouped, truncated = _unresolved_runs(
        journal, page_limit=page_limit, max_pages=max_pages,
    )
    if not grouped:
        # Nothing is unresolved: no owner is claimed and no event is emitted,
        # so ordinary startups do not add operations noise.
        _LOG.debug("startup effect reconciliation: no unresolved effects")
        return StartupReconciliationReport(truncated=truncated)
    if peer_hosts_live is not None:
        try:
            peers = peer_hosts_live() is not False
        except Exception as exc:  # noqa: BLE001 - an unreadable probe presumes a peer
            _LOG.warning("worker effect host probe failed: %s", type(exc).__name__)
            peers = True
        if peers:
            return _finish(
                StartupReconciliationReport(deferred="live-peer-host-process"), emit,
            )
    reports: list[EffectReconciliationReport] = []
    foreign: list[str] = []
    failed: list[tuple[str, str]] = []
    for index, (run_id, intents) in enumerate(grouped.items()):
        if index >= max_runs or monotonic() >= deadline:
            truncated = True
            break
        workers = sorted({intent.worker_id for intent in intents})
        if not all(owns_worker(worker) for worker in workers):
            # Claiming another host's worker identity could fence its live
            # effects; leave the run for its owner or an operator.
            foreign.append(run_id)
            continue
        scopes = {intent.worker_id: intent.scope for intent in intents}
        for worker_id in workers:
            binding = AuthenticatedWorkerBinding(
                journal, run_id, worker_id, owner_epoch, scopes[worker_id],
                auto_reconcile=True,
            )
            try:
                report = binding.reconcile_before_restart(
                    max_records=page_limit,
                    verifier_timeout_seconds=verifier_timeout_seconds,
                    deadline_monotonic=deadline,
                )
            except (EffectJournalError, ValueError) as exc:
                # Storage or bound failures leave every fence in place.
                failed.append((run_id, type(exc).__name__))
                _LOG.warning(
                    "startup effect reconciliation failed: run=%s worker=%s error=%s",
                    run_id, worker_id, type(exc).__name__,
                )
                continue
            reports.append(report)
    return _finish(StartupReconciliationReport(
        tuple(reports), tuple(foreign), tuple(failed), truncated,
    ), emit)


def _finish(
    result: StartupReconciliationReport,
    emit: Callable[[str, dict[str, object]], None] | None,
) -> StartupReconciliationReport:
    summary = result.summary()
    log = _LOG.warning if (
        result.fenced or result.failed_runs or result.foreign_runs
        or result.truncated or result.deferred
    ) else _LOG.info
    log("startup effect reconciliation: %s", summary)
    if emit is not None:
        try:
            emit("worker.effects.reconciled", {
                **summary,
                "resolved_intents": [item.intent_id for item in result.resolved][:64],
                "fenced_intents": [item.intent_id for item in result.fenced][:64],
            })
        except Exception as exc:  # noqa: BLE001 - observability must not unfence
            _LOG.warning("startup effect reconciliation event failed: %s", type(exc).__name__)
    return result


__all__ = [
    "StartupReconciliationReport", "reconcile_unresolved_effects",
]
