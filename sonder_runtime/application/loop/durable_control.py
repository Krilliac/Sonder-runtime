"""Durable control for cancellation, retry, and idempotent loop work.

This module is an application-layer composition of the existing cancellation
tree, retry policy, provider lifecycle, and outbox/CAS ports.  It deliberately
does not sleep, spawn processes, or perform provider I/O.  Adapters supply
those effects through small callbacks and receive immutable evidence back.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib

logger = logging.getLogger(__name__)
import json
from threading import RLock
from typing import Any, Protocol

from ...domain.cancellation_tree import CancellationNode
from ...domain.loop_retry_policy import (
    ReplayAction,
    RetryDecision,
    SideEffectClass,
    retry_decision,
)
from ..cancellation_tree import CancellationTree
from ..persistence.outbox_cas import (
    OutboxCASRepository,
    OutboxEvent,
    TransactionNeutralRecord,
)
from ..ports.specialized_lifecycle import CleanupResult


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class CleanupConformance:
    target_id: str
    cancelled: bool
    quiescent: bool
    resources_released: bool
    detail: str = ""

    @property
    def conforms(self) -> bool:
        return self.cancelled and self.quiescent and self.resources_released


@dataclass(frozen=True, slots=True)
class RetryEvidence:
    operation_id: str
    attempt: int
    failure_code: str
    action: ReplayAction
    classification: str
    delay_cap_seconds: float
    side_effect: SideEffectClass
    evidence_digest: str
    recorded_at: str


class RetryEvidenceLedger:
    """Thread-safe, bounded retention of every admitted retry decision."""

    def __init__(self, *, max_records: int = 256, clock: Callable[[], str] = _now) -> None:
        if isinstance(max_records, bool) or max_records < 1:
            raise ValueError("max_records must be positive")
        self._max_records = max_records
        self._clock = clock
        self._lock = RLock()
        self._records: list[RetryEvidence] = []

    def record(self, operation_id: str, decision: RetryDecision, *, attempt: int = 1, failure_code: str = "") -> RetryEvidence:
        logger.debug(f"RetryEvidenceLedger.record: operation_id={operation_id!r}, attempt={attempt}, action={decision.action.value!r}, failure_code={failure_code!r}")
        if isinstance(attempt, bool) or attempt < 1:
            raise ValueError("attempt must be positive")
        evidence = RetryEvidence(
            operation_id=operation_id,
            attempt=attempt,
            failure_code=str(failure_code),
            action=decision.action,
            classification=decision.classification.value,
            delay_cap_seconds=decision.backoff.cap_for_attempt(1),
            side_effect=decision.side_effect.effect,
            evidence_digest=_digest({
                "operation_id": operation_id,
                "failure_code": str(failure_code),
                "action": decision.action.value,
                "classification": decision.classification.value,
                "side_effect": decision.side_effect.effect.value,
            }),
            recorded_at=self._clock(),
        )
        with self._lock:
            self._records.append(evidence)
            del self._records[:-self._max_records]
        return evidence

    def snapshot(self) -> tuple[RetryEvidence, ...]:
        with self._lock:
            return tuple(self._records)


@dataclass(frozen=True, slots=True)
class IdempotencyReceipt:
    key: str
    fingerprint: str
    status: str
    revision: int
    result: Any = None
    evidence_digest: str = ""


class IdempotencyConflict(ValueError):
    """The same key was presented for a different operation fingerprint."""


class IdempotencyStore(Protocol):
    def begin(self, key: str, fingerprint: str) -> IdempotencyReceipt: ...
    def complete(self, key: str, fingerprint: str, result: Any) -> IdempotencyReceipt: ...
    def mark_unknown(self, key: str, fingerprint: str, *, evidence: Mapping[str, Any]) -> IdempotencyReceipt: ...
    def reconcile(self, key: str, fingerprint: str, result: Any, *, evidence: Mapping[str, Any]) -> IdempotencyReceipt: ...


class OutboxIdempotencyStore:
    """Idempotency records persisted as versioned aggregates plus outbox events."""

    def __init__(self, repository: OutboxCASRepository, *, clock: Callable[[], str] = _now) -> None:
        self._repository = repository
        self._clock = clock
        self._lock = RLock()

    @staticmethod
    def _aggregate_id(key: str) -> str:
        return "idempotency:" + key

    def _read(self, key: str, fingerprint: str) -> TransactionNeutralRecord | None:
        record = self._repository.get(self._aggregate_id(key))
        if record is not None and record.payload.get("fingerprint") != fingerprint:
            raise IdempotencyConflict("idempotency key is bound to a different operation")
        return record

    def _write(self, key: str, fingerprint: str, status: str, *, result: Any = None, evidence: Mapping[str, Any] | None = None) -> IdempotencyReceipt:
        aggregate = self._aggregate_id(key)
        current = self._read(key, fingerprint)
        revision = -1 if current is None else current.revision
        payload = {
            "key": key,
            "fingerprint": fingerprint,
            "status": status,
            "result": result,
            "evidence": dict(evidence or {}),
            "updated_at": self._clock(),
        }
        record = TransactionNeutralRecord(aggregate, revision + 1, payload)
        event = OutboxEvent(
            event_id=f"{aggregate}:{revision + 1}",
            aggregate_id=aggregate,
            event_type="idempotency.changed",
            revision=revision + 1,
            payload=payload,
            occurred_at=payload["updated_at"],
        )
        with self._lock:
            # Re-read under the lock so two callers cannot accept stale CAS state.
            current = self._read(key, fingerprint)
            expected = -1 if current is None else current.revision
            if expected != revision:
                return self._receipt(current)
            written = self._repository.append(record, event, expected_revision=expected)
            if written is None:
                latest = self._read(key, fingerprint)
                if latest is None:
                    logger.critical(f"idempotency CAS lost without a readable record: key={key!r}, aggregate={aggregate!r} — persistence layer state corruption")
                    raise RuntimeError("idempotency CAS lost without a readable record")
                return self._receipt(latest)
            return self._receipt(written)

    @staticmethod
    def _receipt(record: TransactionNeutralRecord | None) -> IdempotencyReceipt:
        if record is None:
            logger.critical("idempotency record is missing when one was expected — persistence layer may be corrupted")
            raise RuntimeError("idempotency record is missing")
        payload = record.payload
        evidence = payload.get("evidence", {})
        return IdempotencyReceipt(
            str(payload["key"]), str(payload["fingerprint"]), str(payload["status"]),
            record.revision, payload.get("result"), _digest(evidence if isinstance(evidence, Mapping) else {}),
        )

    def begin(self, key: str, fingerprint: str) -> IdempotencyReceipt:
        logger.debug(f"OutboxIdempotencyStore.begin: key={key!r}")
        self._validate(key, fingerprint)
        with self._lock:
            current = self._read(key, fingerprint)
            return self._receipt(current) if current else self._write(key, fingerprint, "started")

    def complete(self, key: str, fingerprint: str, result: Any) -> IdempotencyReceipt:
        logger.debug(f"OutboxIdempotencyStore.complete: key={key!r}")
        self._validate(key, fingerprint)
        with self._lock:
            current = self._read(key, fingerprint)
            if current is not None and current.payload.get("status") in {"completed", "reconciled"}:
                return self._receipt(current)
            return self._write(key, fingerprint, "completed", result=result)

    def mark_unknown(self, key: str, fingerprint: str, *, evidence: Mapping[str, Any]) -> IdempotencyReceipt:
        logger.error(f"idempotency outcome uncertain, marking unknown: key={key!r}")
        logger.warning(f"marking idempotency key as unknown (outcome uncertain): key={key!r}")
        logger.debug(f"OutboxIdempotencyStore.mark_unknown: key={key!r}")
        self._validate(key, fingerprint)
        with self._lock:
            current = self._read(key, fingerprint)
            if current is not None and current.payload.get("status") in {"completed", "reconciled"}:
                return self._receipt(current)
            return self._write(key, fingerprint, "unknown", evidence=evidence)

    def reconcile(self, key: str, fingerprint: str, result: Any, *, evidence: Mapping[str, Any]) -> IdempotencyReceipt:
        logger.debug(f"OutboxIdempotencyStore.reconcile: key={key!r}")
        self._validate(key, fingerprint)
        with self._lock:
            current = self._read(key, fingerprint)
            if current is None or current.payload.get("status") not in {"started", "unknown"}:
                raise ValueError("only started or unknown operations may be reconciled")
            return self._write(key, fingerprint, "reconciled", result=result, evidence=evidence)

    @staticmethod
    def _validate(key: str, fingerprint: str) -> None:
        if not str(key).strip() or not str(fingerprint).strip():
            raise ValueError("key and fingerprint must be non-empty")


@dataclass
class _Binding:
    node: CancellationNode
    target_id: str
    cancel: Callable[[str], bool]
    cleanup: Callable[[float | None], CleanupResult]
    cancel_result: bool | None = None
    cleanup_result: CleanupConformance | None = None


class DurableLoopControl:
    """Coordinate cancellation propagation and policy-backed retry decisions."""

    def __init__(self, *, cancellation: CancellationTree | None = None, ledger: RetryEvidenceLedger | None = None) -> None:
        self.cancellation = cancellation or CancellationTree()
        self.ledger = ledger or RetryEvidenceLedger()
        self._bindings: list[_Binding] = []
        self._lock = RLock()

    def bind(self, node_id: str, target_id: str, *, cancel: Callable[[str], bool], cleanup: Callable[[float | None], CleanupResult]) -> None:
        if not target_id.strip() or not callable(cancel) or not callable(cleanup):
            raise ValueError("target_id and lifecycle callbacks are required")
        with self._lock:
            self._bindings.append(_Binding(self.cancellation.node(node_id), target_id, cancel, cleanup))

    def cancel_and_cleanup(self, node_id: str = "root", *, reason: str = "cancellation requested", timeout: float | None = None) -> tuple[CleanupConformance, ...]:
        logger.debug(f"DurableLoopControl.cancel_and_cleanup: node_id={node_id!r}, reason={reason!r}, timeout={timeout}")
        logger.info(f"cancellation and cleanup initiated: node_id={node_id!r}, reason={reason!r}")
        node = self.cancellation.node(node_id)
        changed = node.cancel(reason=reason)
        with self._lock:
            selected = tuple(binding for binding in self._bindings if binding.node.cancelled)
        reports: list[CleanupConformance] = []
        for binding in selected:
            # Cancellation requests are durable state transitions.  Cache the
            # first callback result so replaying the same request cannot turn
            # a successful cancellation into a false report when an adapter
            # correctly returns False for an already-cancelled target.
            if binding.cancel_result is None:
                binding.cancel_result = bool(binding.cancel(reason)) if changed or node.cancelled else False

            # A clean result is terminal for this binding.  Incomplete
            # cleanup remains retryable, which preserves recovery after a
            # bounded timeout without repeating completed side effects.
            if binding.cleanup_result is None or not binding.cleanup_result.conforms:
                result = binding.cleanup(timeout)
                if not isinstance(result, CleanupResult):
                    raise TypeError("cleanup callback must return CleanupResult")
                binding.cleanup_result = CleanupConformance(
                    binding.target_id,
                    binding.cancel_result,
                    result.quiescent,
                    result.resources_released,
                    result.detail,
                )
                if not binding.cleanup_result.conforms:
                    logger.error(f"cleanup did not conform: target_id={binding.target_id!r}, quiescent={result.quiescent}, resources_released={result.resources_released}")
                    logger.warning(f"cleanup did not conform: target_id={binding.target_id!r}, quiescent={result.quiescent}, resources_released={result.resources_released}")
            reports.append(binding.cleanup_result)
        return tuple(reports)

    def retry(self, operation_id: str, *, failure_code: str = "", status: int | None = None, attempt: int = 1, max_attempts: int = 3, outcome_known: bool = True, effect: SideEffectClass = SideEffectClass.NONE, idempotency_key: str | None = None, retry_after_seconds: float | None = None, deadline_seconds: float | None = None) -> RetryDecision:
        logger.debug(f"DurableLoopControl.retry: operation_id={operation_id!r}, failure_code={failure_code!r}, attempt={attempt}/{max_attempts}, effect={effect.value!r}")
        decision = retry_decision(failure_code, status=status, attempt=attempt, max_attempts=max_attempts, outcome_known=outcome_known, effect=effect, idempotency_key=idempotency_key, retry_after_seconds=retry_after_seconds, deadline_seconds=deadline_seconds)
        if decision.action is ReplayAction.RETRY:
            logger.warning(f"retry scheduled: operation_id={operation_id!r}, attempt={attempt}/{max_attempts}, failure_code={failure_code!r}, delay_cap={decision.backoff.cap_for_attempt(1):.1f}s")
        elif decision.action is ReplayAction.DO_NOT_RETRY and attempt > 1:
            logger.error(f"retry exhausted, failing: operation_id={operation_id!r}, attempt={attempt}/{max_attempts}, failure_code={failure_code!r}")
            logger.warning(f"retry exhausted, failing: operation_id={operation_id!r}, attempt={attempt}/{max_attempts}, failure_code={failure_code!r}")
        self.ledger.record(operation_id, decision, attempt=attempt, failure_code=failure_code)
        return decision


__all__ = [
    "CleanupConformance", "DurableLoopControl", "IdempotencyConflict",
    "IdempotencyReceipt", "IdempotencyStore", "OutboxIdempotencyStore",
    "RetryEvidence", "RetryEvidenceLedger",
]
