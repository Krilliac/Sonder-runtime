"""Pure report assembler for ``improvement_report_data``."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


_SEVERITY_RANK: dict[str, int] = {
    "high": 0,
    "medium": 1,
    "low": 2,
    "info": 3,
}


def _int_value(mapping: Mapping[str, Any], key: str, default: int = 0) -> int:
    raw = mapping.get(key, default)
    return int(raw) if isinstance(raw, (int, float)) else default


def _float_value(mapping: Mapping[str, Any], key: str, default: float = 0.0) -> float:
    raw = mapping.get(key, default)
    return float(raw) if isinstance(raw, (int, float)) else default


def assemble_improvement_report(
    *,
    context: Mapping[str, Any],
    learning_state: Mapping[str, Any],
    gated_positive: float | None,
    acceptance_basis: str,
    autopilot_available: bool,
    autopilot: Mapping[str, Any],
    mcp_state: Mapping[str, Any],
    mcp_recovery_action: str,
    cloud_allowed: bool,
    manifest_text: str,
) -> dict[str, Any]:
    """Assemble the machine-readable next-step self-improvement report."""
    raw_quality = learning_state.get("quality")
    quality: Mapping[str, Any] = raw_quality if isinstance(raw_quality, Mapping) else {}
    interactions = _int_value(learning_state, "interactions", 0)
    outcomes = _int_value(learning_state, "outcomes", 0)
    lesson_count = _int_value(learning_state, "lessons", 0)
    fact_count = _int_value(learning_state, "facts", 0)
    reviewed = _int_value(learning_state, "reviewed_outcomes", 0)
    acceptance = (
        gated_positive / 100.0 if gated_positive is not None else None
    )
    issues: list[dict[str, str]] = []

    def add(area: str, severity: str, title: str, action: str) -> None:
        issues.append({
            "area": area,
            "severity": severity,
            "title": title,
            "action": action,
        })

    if interactions and _float_value(learning_state, "outcome_coverage_percent", 0.0) < 35.0:
        add(
            "learning",
            "high",
            "Too few interactions have grounded outcomes.",
            "Use /accept, /edited, /copied, /pass, /fail, or record_outcome after real use.",
        )
    if interactions == 0:
        add(
            "learning",
            "medium",
            "No learning interactions have been captured yet.",
            "Ask through Sonder Runtime or run /train so answers can become local lessons.",
        )
    if lesson_count < 10:
        add(
            "memory",
            "medium",
            "Lesson memory is still thin.",
            "Run grounded practice or teach examples from known-good work.",
        )
    if quality.get("exact_duplicate_prunable", 0):
        add(
            "memory",
            "medium",
            "Duplicate lessons can be pruned.",
            "Run memory_quality_repair(apply=True) or /qualityfix apply after review.",
        )
    if quality.get("path_or_secret_like", 0):
        add(
            "privacy",
            "high",
            "Some lessons look like they may contain paths or secrets.",
            "Run /privacy for redacted IDs, then dry-run memory_privacy_repair before any explicit cleanup.",
        )
    if quality.get("no_embedding", 0):
        add(
            "memory",
            "medium",
            "%d lessons are missing semantic embeddings." % _int_value(quality, "no_embedding", 0),
            "Run memory_embedding_backfill(apply=False), then backfill a bounded batch with apply=True.",
        )
    stale_embeddings = (
        _int_value(quality, "embedding_legacy", 0)
        + _int_value(quality, "embedding_model_mismatch", 0)
        + _int_value(quality, "embedding_revision_mismatch", 0)
        + _int_value(quality, "embedding_dimension_missing", 0)
        + _int_value(quality, "embedding_dimension_invalid", 0)
    )
    if stale_embeddings:
        add(
            "memory",
            "medium",
            "%d lesson embeddings lack current provenance or are incompatible."
            % stale_embeddings,
            "Run memory_embedding_backfill(apply=False), then refresh bounded "
            "batches locally with apply=True.",
        )
    if quality.get("vague_without_anchor", 0):
        add(
            "memory",
            "low",
            "Some lessons are vague and lack concrete anchors.",
            "Prefer lessons naming APIs, files, errors, commands, or explicit patterns.",
        )
    if quality.get("missing_fts", 0) or quality.get("orphan_fts", 0):
        add(
            "store",
            "medium",
            "Search index drift was detected.",
            "Run self_heal_check and repair the store before large practice batches.",
        )
    if context.get("status") == "hot":
        add(
            "context",
            "medium",
            "The active conversation is near the context limit.",
            "Start a new session or let summaries compress older turns before continuing.",
        )
    raw_runs = autopilot.get("runs", [])
    runs = raw_runs if isinstance(raw_runs, list) else []
    autonomous_attention = sum(
        1 for row in runs
        if isinstance(row, Mapping) and row.get("status") in ("blocked", "interrupted")
    )
    if autonomous_attention:
        add(
            "autonomy",
            "medium",
            "%d autonomous run(s) need explicit review or resume."
            % autonomous_attention,
            "Inspect /autopilot status, then deliberately resume, cancel, or revise the goal.",
        )
    if not autopilot_available:
        add(
            "autonomy",
            "medium",
            "Autopilot status is unavailable.",
            "Run /autopilot status after the store recovers; do not treat missing status as no active work.",
        )
    if mcp_state.get("last_error"):
        raw_provenance = mcp_state.get("provenance")
        provenance: Mapping[str, Any] = (
            raw_provenance if isinstance(raw_provenance, Mapping) else {}
        )
        add(
            "runtime",
            "high",
            (
                "The MCP process is attached to a stale runtime source root."
                if provenance.get("issue") == "stale_source_root"
                else "The latest MCP source refresh failed closed."
            ),
            mcp_recovery_action or (
                "Run /mcp status, fix the reported source error, then use "
                "/mcp refresh; the last known-good tools remain active."
            ),
        )
    elif mcp_state.get("source_changed"):
        add(
            "runtime",
            "medium",
            "The MCP process has newer source waiting to be loaded.",
            "Run /mcp refresh or any MCP tool/list request to publish the update atomically.",
        )
    if mcp_state.get("last_notification_error"):
        add(
            "runtime",
            "low",
            "The MCP client did not accept the latest tool-list notification.",
            "Use /mcp status and reconnect only if the client does not relist tools automatically.",
        )
    if not cloud_allowed:
        add(
            "deployment",
            "info",
            "Hosted tiers are disabled, preserving the local privacy promise.",
            "Enable hosted/cloud tiers only when you intentionally want prompts to leave this machine.",
        )
    if "ground_artifact" not in manifest_text or "artifact_ground" not in manifest_text:
        add(
            "grounding",
            "medium",
            "General or format-specific artifact grounding is not advertised.",
            "Expose ground_artifact and artifact_ground so both in-memory content and real files can be validated.",
        )
    if acceptance_basis == "reviewed" and gated_positive is not None and gated_positive < 60.0:
        add(
            "learning",
            "medium",
            "Caller-judged work succeeds %.1f%% of the time (%d reviewed outcome(s))."
            % (gated_positive, reviewed),
            "This is the honest hit rate; the blended figure is inflated by "
            "autograded self-marking. Review roughly half of delegated output, "
            "and record negatives -- record_outcome with a failing signal is "
            "what the store is starved of.",
        )
    elif acceptance_basis == "unmeasured" and outcomes >= 200:
        unclassifiable = _int_value(learning_state, "unknown_source_outcomes", 0)
        detail = "Almost no outcomes have been judged by a caller (%d of %d)." % (
            reviewed, outcomes,
        )
        if unclassifiable:
            detail += (
                " %d of those predate outcome provenance, so who judged them "
                "is not recoverable -- they are not evidence either way."
                % unclassifiable
            )
        add(
            "learning",
            "low",
            detail,
            "Autograded outcomes cannot tell you whether delegated work is any "
            "good. Use record_outcome after real use so the reviewed rate means "
            "something.",
        )

    if not issues:
        add(
            "system",
            "info",
            "No urgent improvement items detected.",
            "Keep collecting grounded outcomes and periodically run /quality.",
        )

    issues.sort(
        key=lambda item: (
            _SEVERITY_RANK.get(item["severity"], 9),
            item["area"],
            item["title"],
        )
    )
    score = max(0, min(100, int(round(
        100
        - 18 * sum(1 for i in issues if i["severity"] == "high")
        - 9 * sum(1 for i in issues if i["severity"] == "medium")
        - 4 * sum(1 for i in issues if i["severity"] == "low")
    ))))
    if outcomes >= 200 and acceptance_basis == "unmeasured":
        score = min(score, 75)

    return {
        "score": score,
        "interactions": interactions,
        "outcomes": outcomes,
        "acceptance_percent": round(acceptance * 100.0, 1) if acceptance is not None else None,
        "acceptance_basis": acceptance_basis,
        "reviewed_outcomes": learning_state.get("reviewed_outcomes", 0),
        "reviewed_positive_percent": learning_state.get("reviewed_positive_percent", 0.0),
        "autograded_outcomes": learning_state.get("autograded_outcomes", 0),
        "unknown_source_outcomes": learning_state.get("unknown_source_outcomes", 0),
        "lessons": lesson_count,
        "facts": fact_count,
        "cloud_allowed": cloud_allowed,
        "context_status": context.get("status", "unknown"),
        "memory_quality": {
            "duplicates": quality.get("exact_duplicate_prunable", 0),
            "vague": quality.get("vague_without_anchor", 0),
            "no_embedding": quality.get("no_embedding", 0),
            "path_or_secret_like": quality.get("path_or_secret_like", 0),
            "fts_issues": (
                _int_value(quality, "missing_fts", 0)
                + _int_value(quality, "orphan_fts", 0)
            ),
        },
        "learning_health": dict(learning_state),
        "autopilot": {
            "available": autopilot_available,
            "active": autopilot.get("active_runs", 0),
            "resumable": autopilot.get("resumable_runs", 0),
            "database": autopilot.get("database", ""),
        },
        "mcp_runtime": dict(mcp_state),
        "issues": issues,
    }
