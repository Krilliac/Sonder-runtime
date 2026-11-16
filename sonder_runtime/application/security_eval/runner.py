"""Run the security-hypothesis eval: item mode (judge one labelled hypothesis) or chunk mode.

One call does a bounded slice of the work and always leaves a consistent report: rows are appended to
the store one by one, the run stops between items (limit, time budget, cancellation) and never in the
middle of a model call, and a later call with the same output directory resumes where this one stopped.
A per-item failure becomes the item's outcome and the run continues; only setup problems raise
(bad repo root, residency refusal, a gateway policy refusal, a configuration mismatch, a bad template).
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from ...domain.common.errors import Forbidden, InvalidInput, SonderError
from ...domain.security_eval.context_select import HELPER_VERSION, ContextBundle
from ...domain.security_eval.errors import ResidencyRefused, ResumeMismatch
from ...domain.security_eval.prompts import (
    DEFAULT_CHUNK_TEMPLATE, DEFAULT_ITEM_TEMPLATE, DEFAULT_SYSTEM_PROMPT, chunk_prompt, item_prompt,
    render_task)
from ...domain.security_eval.records import MAX_RANGE_SPAN, HypothesisRecord, LineRange
from ...domain.security_eval.scoring import (
    ChunkOutcome, ScoreRow, match_findings, paired_comparison, score, score_chunks)
from ...domain.security_eval.summary import render_summary
from ...domain.security_eval.verdict import VerdictParseError, parse_hypotheses, parse_verdict
from .ports import Clock, ModelCall, Repo, RepoProvider, ResultStore, SourceWindow, VerdictModel

ARMS = ("none", "retrieved")
MODES = ("item", "chunk")
_SOURCE_ERRORS = (SonderError, OSError, ValueError)
_MAX_FINDINGS = 50


@dataclass(frozen=True)
class RunPlan:
    """What one call does. ``identity`` is everything that changes a row's meaning (it is hashed)."""

    requested_model: str
    identity: Mapping[str, object]
    mode: str = "item"
    arms: tuple[str, ...] = ARMS
    limit: int = 0
    time_budget_seconds: float = 1500.0
    pad_lines: int = 20
    template: str | None = None
    keep_raw_chars: int = 0
    chunk_chars: int = 9500
    match_line_tolerance: int = 5
    categories: frozenset[str] = frozenset()
    stages: frozenset[str] = frozenset()
    hids: frozenset[str] = frozenset()
    dataset_sha256: str = ""
    invalid_lines: int = 0
    cancelled: Callable[[], bool] | None = None

    @property
    def digest(self) -> str:
        blob = json.dumps(dict(self.identity), sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class RunReport:
    complete: bool
    stopped_reason: str  # done | limit | time_budget | cancelled | score_only
    remaining: int
    processed: int
    skipped_done: int
    report: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Unit:
    hid: str
    arm: str
    kind: str
    repo: Repo
    record: HypothesisRecord | None = None
    window: SourceWindow | None = None
    scope: tuple[HypothesisRecord, ...] = ()
    file: str = ""
    error: Exception | None = None

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.hid, self.arm, self.kind)


def _chunk_id(root: str, file: str, span: str) -> str:
    return "chunk:%s:%s:%s" % (hashlib.sha1(root.encode("utf-8")).hexdigest()[:8], file, span)


class SecurityEvalRunner:
    """Composes the ports; holds no state between calls (the store is the state)."""

    def __init__(self, *, repos: RepoProvider, model: VerdictModel | None, store: ResultStore,
                 clock: Clock) -> None:
        self._repos, self._model, self._store, self._clock = repos, model, store, clock

    def run(self, records: Sequence[HypothesisRecord], plan: RunPlan) -> RunReport:
        return self._execute(records, plan, call_model=True)

    def score_only(self, records: Sequence[HypothesisRecord], plan: RunPlan) -> RunReport:
        """Rescore ``items.jsonl`` and rewrite the report; makes no model call."""
        return self._execute(records, plan, call_model=False)

    def _require_model(self) -> VerdictModel:
        if self._model is None:
            raise RuntimeError("this run needs a model and none was supplied")
        return self._model

    # -- planning -------------------------------------------------------------------------
    @staticmethod
    def _validate(plan: RunPlan) -> None:
        if plan.mode not in MODES:
            raise InvalidInput("mode must be one of %s" % ", ".join(MODES))
        if not plan.arms or any(arm not in ARMS for arm in plan.arms) or len(set(plan.arms)) != len(plan.arms):
            raise InvalidInput("arms must be a non-empty subset of %s" % ", ".join(ARMS))
        if plan.mode == "item":
            render_task(plan.template or DEFAULT_ITEM_TEMPLATE, {"category": "c", "file": "f", "lines": "1"})
        else:
            render_task(DEFAULT_CHUNK_TEMPLATE, {"file": "f", "lines": "1-2"})

    @staticmethod
    def _select(records: Sequence[HypothesisRecord], plan: RunPlan) -> list[HypothesisRecord]:
        return [r for r in records
                if (not plan.categories or r.category in plan.categories)
                and (not plan.stages or r.stage in plan.stages)
                and (not plan.hids or r.hid in plan.hids)]

    def _units(self, selected: Sequence[HypothesisRecord], plan: RunPlan,
               repos: Mapping[str, Repo]) -> list[_Unit]:
        units: list[_Unit] = []
        if plan.mode == "item":
            for record in selected:
                for arm in plan.arms:
                    units.append(_Unit(record.hid, arm, "item", repos[record.repo_root], record=record))
            return units
        groups: dict[tuple[str, str], list[HypothesisRecord]] = {}
        for record in selected:
            groups.setdefault((record.repo_root, record.file), []).append(record)
        for (root, file), group in groups.items():
            repo = repos[root]
            try:
                windows = repo.read_chunks(file, plan.chunk_chars)
            except _SOURCE_ERRORS as exc:
                scope = tuple(group)
                for arm in plan.arms:
                    units.append(_Unit(_chunk_id(root, file, "*"), arm, "chunk", repo, scope=scope,
                                       file=file, error=exc))
                continue
            for window in windows:
                scope = tuple(r for r in group if window.start <= r.lines.start <= window.end)
                for arm in plan.arms:
                    units.append(_Unit(_chunk_id(root, file, "%d-%d" % (window.start, window.end)), arm,
                                       "chunk", repo, window=window, scope=scope, file=file))
        return units

    def _execute(self, records: Sequence[HypothesisRecord], plan: RunPlan, *, call_model: bool) -> RunReport:
        self._validate(plan)
        digest = plan.digest
        rows = self._store.read_rows()
        foreign = [row for row in rows if row.get("config_digest") != digest]
        if foreign:
            raise ResumeMismatch(
                "the output directory holds %d row(s) from another configuration (digest %s, this run %s); "
                "use a new out_dir or restore the original settings" % (
                    len(foreign), foreign[0].get("config_digest", "?"), digest))
        selected = self._select(records, plan)
        repos = {root: self._repos(root) for root in dict.fromkeys(r.repo_root for r in selected)}
        units = self._units(selected, plan, repos)
        done = {(row.get("hid"), row.get("arm"), row.get("mode", "item")) for row in rows
                if row.get("model") == plan.requested_model}
        pending = [unit for unit in units if unit.key not in done]
        skipped_done = len(units) - len(pending)
        processed = 0
        stopped = "done"
        if not call_model:
            stopped = "score_only"
        else:
            if pending:
                self._require_model().preflight()
            started = self._clock.now()
            for unit in pending:
                if plan.cancelled is not None and plan.cancelled():
                    stopped = "cancelled"
                    break
                if plan.limit and processed >= plan.limit:
                    stopped = "limit"
                    break
                if self._clock.now() - started >= plan.time_budget_seconds:
                    stopped = "time_budget"
                    break
                self._store.append(self._run_unit(unit, plan, digest))
                processed += 1
        remaining = len(pending) - processed
        report = self._report(self._store.read_rows(), plan, digest, remaining == 0, stopped, remaining)
        self._store.write_report(report, render_summary(report))
        return RunReport(remaining == 0, stopped, remaining, processed, skipped_done, report)

    # -- one unit -------------------------------------------------------------------------
    def _base_row(self, unit: _Unit, plan: RunPlan, digest: str) -> dict[str, Any]:
        row: dict[str, Any] = {"hid": unit.hid, "arm": unit.arm, "mode": unit.kind, "model": plan.requested_model,
               "config_digest": digest, "outcome": "SCORED"}
        if unit.record is not None:
            record = unit.record
            row.update({"category": record.category, "stage": record.stage, "label": record.label,
                        "file": record.file, "lines": record.lines.as_text()})
        else:
            window = unit.window
            row.update({"category": "", "stage": "", "label": "", "file": unit.file,
                        "lines": "%d-%d" % (window.start, window.end) if window is not None else "*",
                        "scope_confirmed": sum(1 for r in unit.scope if r.label == "CONFIRMED")})
        return row

    @staticmethod
    def _failed(row: dict[str, Any], outcome: str, exc: BaseException) -> dict[str, Any]:
        row.update({"outcome": outcome, "error_type": type(exc).__name__, "error": str(exc)[:200]})
        return row

    @staticmethod
    def _context_text(bundle: ContextBundle | None, row: dict[str, Any]) -> str:
        if bundle is None:
            row.update({"context_chars": 0, "context_items": 0, "context_truncated": False})
            return ""
        row.update({"context_chars": len(bundle.text), "context_items": len(bundle.items),
                    "context_truncated": bundle.truncated})
        if bundle.skipped:
            row["context_skipped"] = {key: list(value) for key, value in sorted(bundle.skipped.items())}
        if bundle.reason:
            row["context_reason"] = bundle.reason
        return bundle.text

    @staticmethod
    def _call_fields(row: dict[str, Any], call: ModelCall, plan: RunPlan) -> None:
        row.update({"served_model": call.served_model, "served_model_source": call.served_model_source,
                    "tokens_in": call.tokens_in, "tokens_out": call.tokens_out,
                    "token_source": call.token_source, "duration_ms": call.duration_ms})
        if plan.keep_raw_chars > 0:
            row["raw"] = call.text[:plan.keep_raw_chars]

    def _run_unit(self, unit: _Unit, plan: RunPlan, digest: str) -> dict[str, Any]:
        row = self._base_row(unit, plan, digest)
        if unit.error is not None:
            return self._failed(row, "SOURCE_ERROR", unit.error)
        if unit.kind == "item":
            return self._run_item(unit, plan, row)
        return self._run_chunk(unit, plan, row)

    def _ask(self, row: dict[str, Any], prompt: str, plan: RunPlan) -> ModelCall | None:
        """The model call; ``None`` after recording MODEL_ERROR. Policy refusals abort the run."""
        try:
            call = self._require_model().ask(DEFAULT_SYSTEM_PROMPT, prompt)
        except ResidencyRefused:
            raise
        except Forbidden:
            raise
        except Exception as exc:  # noqa: BLE001 - a per-item model failure is the item's outcome
            self._failed(row, "MODEL_ERROR", exc)
            return None
        self._call_fields(row, call, plan)
        return call

    def _run_item(self, unit: _Unit, plan: RunPlan, row: dict[str, Any]) -> dict[str, Any]:
        record, repo = unit.record, unit.repo
        if record is None:
            raise RuntimeError("an item unit carries its record")
        try:
            window = repo.read_window(record.file, record.lines, plan.pad_lines)
        except _SOURCE_ERRORS as exc:
            return self._failed(row, "SOURCE_ERROR", exc)
        bundle = None
        if unit.arm == "retrieved":
            try:
                bundle = repo.retrieve(record.file, record.lines)
            except Exception as exc:  # noqa: BLE001 - a retrieval fault must not abort the run
                return self._failed(row, "CONTEXT_ERROR", exc)
        context = self._context_text(bundle, row)
        prompt = item_prompt(record, window.text, window.start, context, plan.template)
        call = self._ask(row, prompt, plan)
        if call is None:
            return row
        try:
            parsed = parse_verdict(call.text)
        except VerdictParseError as exc:
            return self._failed(row, "UNPARSED", exc)
        row.update({"verdict": parsed.verdict, "confidence": parsed.confidence, "reason": parsed.reason,
                    "parse_mode": parsed.parse_mode})
        return row

    def _run_chunk(self, unit: _Unit, plan: RunPlan, row: dict[str, Any]) -> dict[str, Any]:
        window, repo = unit.window, unit.repo
        if window is None:
            raise RuntimeError("a chunk unit without an error carries its window")
        bundle = None
        if unit.arm == "retrieved":
            try:
                span = LineRange(window.start, min(window.end, window.start + MAX_RANGE_SPAN - 1))
                bundle = repo.retrieve(unit.file, span)
            except Exception as exc:  # noqa: BLE001
                return self._failed(row, "CONTEXT_ERROR", exc)
        context = self._context_text(bundle, row)
        call = self._ask(row, chunk_prompt(unit.file, window.start, window.text, context, None), plan)
        if call is None:
            return row
        try:
            findings = parse_hypotheses(call.text, _MAX_FINDINGS)
        except VerdictParseError as exc:
            return self._failed(row, "UNPARSED", exc)
        matched = match_findings(findings, unit.scope, plan.match_line_tolerance, unit.file)
        row.update({"generated": len(findings), "matched_hids": [r.hid for r in matched],
                    "matched_labels": [r.label for r in matched]})
        return row

    # -- report ---------------------------------------------------------------------------
    @staticmethod
    def _report(rows: list[dict[str, Any]], plan: RunPlan, digest: str, complete: bool, stopped: str,
                remaining: int) -> dict[str, Any]:
        mine = [row for row in rows if row.get("config_digest") == digest]
        items = [row for row in mine if row.get("mode", "item") == "item"]
        chunks = [row for row in mine if row.get("mode") == "chunk"]
        score_rows = [ScoreRow(str(row.get("hid")), str(row.get("arm")), str(row.get("category", "")),
                               str(row.get("stage", "")), str(row.get("label", "")), row.get("verdict"),
                               str(row.get("outcome", "SCORED"))) for row in items]
        arms_seen = sorted({r.arm for r in score_rows} | {str(row.get("arm")) for row in chunks})
        called = [row for row in mine if row.get("served_model")]
        measured = [row for row in called if row.get("token_source") == "measured"]
        report: dict[str, Any] = {
            "schema": 1, "mode": plan.mode, "requested_model": plan.requested_model,
            "helper_version": HELPER_VERSION, "config_digest": digest, "config": dict(plan.identity),
            "arms": arms_seen, "dataset_sha256": plan.dataset_sha256,
            "invalid_dataset_lines": plan.invalid_lines, "complete": complete,
            "stopped_reason": stopped, "remaining": remaining, "rows": len(mine),
            "outcomes": dict(sorted(Counter(str(row.get("outcome", "SCORED")) for row in mine).items())),
            "parse_modes": dict(sorted(Counter(str(row["parse_mode"]) for row in mine if row.get("parse_mode")).items())),
            "served_models": dict(sorted(Counter(str(row["served_model"]) for row in called).items())),
            "served_model_source": next((str(row["served_model_source"]) for row in called
                                         if row.get("served_model_source")), "gateway_resolved"),
            "tokens": {"tokens_in": sum(int(row.get("tokens_in") or 0) for row in called),
                       "tokens_out": sum(int(row.get("tokens_out") or 0) for row in called),
                       "rows": len(called), "rows_measured": len(measured),
                       "rows_unverified": len(called) - len(measured)},
            "scores": score(score_rows).as_dict() if score_rows else {"overall": {}, "groups": {}},
            "paired": (paired_comparison(score_rows, "none", "retrieved").as_dict()
                       if {"none", "retrieved"} <= {r.arm for r in score_rows} else None),
        }
        if chunks:
            outcomes = [ChunkOutcome(str(row.get("arm")), int(row.get("generated") or 0),
                                     tuple(row.get("matched_labels") or ()),
                                     int(row.get("scope_confirmed") or 0), str(row.get("outcome", "SCORED")))
                        for row in chunks]
            report["chunk_scores"] = {arm: value.as_dict() for arm, value in sorted(score_chunks(outcomes).items())}
        return report
