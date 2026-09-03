"""Serializable fanout receipts built from the durable run store.

A receipt reports counts, usage, skips, answers and failures for one fanout
run without exposing the sealed prompt, deriving any cooldown as a live
relative delay so a stored absolute expiry never leaks. It reads the fanout
store, so it lives with the adapters. Moved from ``server.py`` in the WP1
Three-Hundred-Twenty-Sixth Slice with its behaviour byte-for-byte intact.
"""
from __future__ import annotations

import time

from sonder_runtime.adapters.persistence import fanout_store
from sonder_runtime.domain.fanout_admission import fanout_limits


def build_receipt(run_id, *, admission):
    """Build a serializable receipt without exposing the sealed prompt.

    ``admission(run, rows, limits)`` describes the immutable request envelope;
    it is injected so the root delegate keeps the routing classifiers' seams.
    """
    run = fanout_store.get_run(run_id)
    if run is None:
        return None
    limits = fanout_limits(run)
    rows = fanout_store.list_results(run_id)
    now = time.time()
    def result_usage(row):
        truncation_known = bool(row.get("answer_truncation_known"))
        return {
            # Legacy receipts never recorded source size.  Do not infer that a
            # 64k prefix was complete: callers get an explicit unknown instead.
            "answer_chars": max(0, int(row.get("answer_chars") or 0)) if truncation_known else None,
            "stored_answer_chars": len(row.get("answer") or ""),
            "answer_truncation_known": truncation_known,
            "answer_truncated": bool(row.get("answer_truncated")) if truncation_known else None,
            "thinking_chars": max(0, int(row.get("thinking_chars") or 0)),
            "done_reason": row.get("done_reason") or None,
        }

    answers = [{"model": row["model"], "answer": row["answer"], "elapsed_ms": row["elapsed_ms"],
                **result_usage(row)}
               for row in rows if row["status"] == "answered"]
    def failure_receipt(row):
        item = {
            "model": row["model"],
            "error": row["error"],
            "elapsed_ms": row["elapsed_ms"],
            "status": row["status"],
            # Null means a legacy receipt predates the closed vocabulary.
            "failure_class": fanout_store.normalize_failure_class(row.get("failure_class")),
            **result_usage(row),
        }
        # The database stores an absolute expiry so a process restart cannot
        # turn a provider hint into a longer wait.  The public receipt gets
        # only a live relative delay; it is informative and never causes an
        # automatic replay of this terminal failed row.
        try:
            expiry = float(row.get("retry_after_ts"))
            # A past-but-valid provider hint remains observable as zero.  This
            # distinguishes it from no provider hint at all without exposing
            # the absolute timestamp.
            remaining_ms = max(0, int((expiry - now) * 1000))
        except (TypeError, ValueError, OverflowError):
            remaining_ms = None
        if remaining_ms is not None:
            item["retry_after_ms"] = remaining_ms
        return item

    failures = [failure_receipt(row) for row in rows if row["status"] in ("failed", "unknown")]
    failed_rows = [row for row in rows if row["status"] == "failed"]
    unknown_rows = [row for row in rows if row["status"] == "unknown"]
    pending_rows = [row for row in rows if row["status"] == "pending"]
    running_rows = [row for row in rows if row["status"] == "running"]
    execution_skips = [{"model": row["model"], "reason": row["error"] or "not executed"}
                       for row in rows if row["status"] == "skipped"]
    ended = run.get("finished_ts") or now
    plan_skips = []
    for row in limits["plan_skipped"]:
        item = dict(row) if isinstance(row, dict) else {"reason": str(row or "not eligible")}
        expiry = item.pop("retry_after_ts", None)
        try:
            remaining_ms = int((float(expiry) - now) * 1000)
        except (TypeError, ValueError):
            remaining_ms = 0
        if remaining_ms > 0:
            item["retry_after_ms"] = remaining_ms
        plan_skips.append(item)
    answered_rows = [row for row in rows if row["status"] == "answered"]
    known_answer_rows = [row for row in answered_rows if row.get("answer_truncation_known")]
    return {
        "run_id": run["id"],
        "status": run["status"],
        "scope": run["scope"],
        "selection_profile": limits["selection_profile"] or None,
        "models_selected": len(rows),
        "models_answered": len(answers),
        # ``unknown`` means the host cannot prove whether an in-flight
        # provider request was sent. Keep it separate from ordinary failures
        # so retry_unknown remains an explicit metered replay decision.
        "models_failed": len(failed_rows),
        "models_unknown": len(unknown_rows),
        # These make an active durable receipt usable as a progress report.
        # They are scalar-only; model ids and answers remain owner-scoped in
        # the detailed arrays below.
        "models_pending": len(pending_rows),
        "models_running": len(running_rows),
        "models_skipped": len(plan_skips) + len(execution_skips),
        "skipped": plan_skips + execution_skips,
        "resident_before": limits["resident_before"],
        "resident_snapshot_known": limits["resident_snapshot_known"],
        "total_elapsed_ms": max(0, int((float(ended) - float(run["created_ts"])) * 1000)),
        "cloud_workers": limits["cloud_workers"],
        "usage": {
            # Total source output is exact only if every answered receipt was
            # recorded after the metric migration.
            "answer_chars": (
                sum(max(0, int(row.get("answer_chars") or 0)) for row in known_answer_rows)
                if len(known_answer_rows) == len(answered_rows) else None
            ),
            "stored_answer_chars": sum(len(row.get("answer") or "") for row in answered_rows),
            "answer_chars_known_models": len(known_answer_rows),
            "thinking_chars": sum(max(0, int(row.get("thinking_chars") or 0)) for row in rows),
            "models_with_observed_thinking": sum(
                1 for row in rows if int(row.get("thinking_chars") or 0) > 0
            ),
        },
        "admission": admission(run, rows, limits),
        "answers": sorted(answers, key=lambda row: row["model"].casefold()),
        "failures": sorted(failures, key=lambda row: row["model"].casefold()),
    }
