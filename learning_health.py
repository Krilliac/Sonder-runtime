"""Structured, read-only health metrics for Sonder's learning loop."""

from __future__ import annotations

import memory_quality
import memory_store
import retriever
import reward


def _percent(numerator: int | float, denominator: int | float) -> float:
    if not denominator:
        return 0.0
    return round(100.0 * float(numerator) / float(denominator), 1)


def _lesson_source(source: str | None, grounded: bool) -> str:
    if grounded:
        return "interaction"
    source = str(source or "").strip()
    if not source:
        return "unknown"
    return source.split(":", 1)[0]


def _lesson_sources(conn) -> tuple[dict[str, int], int]:
    rows = conn.execute(
        "SELECT l.source_interaction AS source, "
        "CASE WHEN i.id IS NULL THEN 0 ELSE 1 END AS grounded "
        "FROM lessons l LEFT JOIN interactions i "
        "ON i.id=l.source_interaction"
    ).fetchall()
    sources: dict[str, int] = {}
    grounded = 0
    for row in rows:
        is_grounded = bool(row["grounded"])
        bucket = _lesson_source(row["source"], is_grounded)
        sources[bucket] = sources.get(bucket, 0) + 1
        grounded += int(is_grounded)
    return dict(sorted(sources.items())), grounded


def _outcome_metrics(conn) -> dict:
    rows = conn.execute(
        "SELECT signal, COUNT(*) AS count, AVG(reward) AS average_reward "
        "FROM outcomes GROUP BY signal"
    ).fetchall()
    signals = []
    outcomes = 0
    good_outcomes = 0
    for row in rows:
        signal = str(row["signal"])
        count = int(row["count"] or 0)
        good = reward.is_good(signal)
        signals.append(
            {
                "signal": signal,
                "count": count,
                "average_reward": round(float(row["average_reward"] or 0.0), 3),
                "good": good,
            }
        )
        outcomes += count
        if good:
            good_outcomes += count
    signals.sort(key=lambda item: (-item["count"], item["signal"]))
    outcome_interactions = int(
        conn.execute(
            "SELECT COUNT(DISTINCT interaction_id) FROM outcomes"
        ).fetchone()[0]
    )
    good_signals = sorted(signal for signal in reward.VALID_SIGNALS if reward.is_good(signal))
    good_interactions = 0
    if good_signals:
        placeholders = ",".join("?" for _ in good_signals)
        good_interactions = int(
            conn.execute(
                "SELECT COUNT(DISTINCT interaction_id) FROM outcomes "
                "WHERE signal IN (%s)" % placeholders,
                tuple(good_signals),
            ).fetchone()[0]
        )
    return {
        "outcomes": outcomes,
        "outcome_interactions": outcome_interactions,
        "good_outcomes": good_outcomes,
        "bad_outcomes": outcomes - good_outcomes,
        "good_outcome_interactions": good_interactions,
        "positive_percent": _percent(good_outcomes, outcomes),
        "signals": signals,
    }


def _lesson_outcome_metrics(conn) -> dict:
    stats = memory_store.lesson_usage_stats(conn)
    evaluated = 0
    with_losses = 0
    loss_only = 0
    quarantined = 0
    quarantine_details = []
    for lesson_id, row in stats.items():
        wins = int(row.get("wins") or 0)
        losses = int(row.get("losses") or 0)
        if wins + losses:
            evaluated += 1
        if losses:
            with_losses += 1
        if losses and not wins:
            loss_only += 1
        decision = retriever.lesson_quarantine(row)
        if decision.get("active"):
            quarantined += 1
            if len(quarantine_details) < 10:
                quarantine_details.append({
                    "lesson_id": lesson_id,
                    "losses_since_win": decision.get("losses_since_win", 0),
                    "distinct_loss_tasks_since_win": decision.get(
                        "distinct_loss_tasks_since_win", 0
                    ),
                    "avg_reward_since_win": decision.get(
                        "avg_reward_since_win"
                    ),
                    "last_failure_ts": decision.get("last_failure_ts"),
                    "retry_after": decision.get("retry_after"),
                })
    return {
        "evaluated_lessons": evaluated,
        "lessons_with_losses": with_losses,
        "loss_only_lessons": loss_only,
        "quarantined_lessons": quarantined,
        "quarantined_lesson_details": quarantine_details,
        "quarantine_review": (
            "Lessons automatically re-enter probation after retry_after; "
            "a grounded success resets the evidence epoch."
        ),
    }


def _status(report: dict) -> str:
    quality = report["quality"]
    severe = (
        quality["path_or_secret_like"]
        + quality["missing_fts"]
        + quality["orphan_fts"]
    )
    if severe or (report["outcomes"] and report["positive_percent"] < 60.0):
        return "attention"
    hygiene = (
        quality["exact_duplicate_prunable"]
        + quality["no_embedding"]
        + quality["vague_without_anchor"]
        + quality["missing_source_interaction"]
    )
    if hygiene or (
        report["interactions"] >= 20
        and report["outcome_coverage_percent"] < 35.0
    ) or (report["outcomes"] and report["positive_percent"] < 80.0) or (
        report.get("quarantined_lessons", 0)
    ):
        return "watch"
    if not report["interactions"] or not report["outcomes"] or not report["lessons"]:
        return "building"
    return "healthy"


def build_report(conn) -> dict:
    """Build one stable JSON-ready learning and memory-health snapshot."""
    interactions = int(conn.execute("SELECT COUNT(*) FROM interactions").fetchone()[0])
    lessons = int(conn.execute("SELECT COUNT(*) FROM lessons").fetchone()[0])
    facts = int(conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0])
    sources, grounded_lessons = _lesson_sources(conn)
    outcomes = _outcome_metrics(conn)
    lesson_outcomes = _lesson_outcome_metrics(conn)
    audit = memory_quality.audit(conn)
    quality = {
        "exact_duplicate_groups": int(audit.get("exact_duplicate_groups", 0)),
        "exact_duplicate_prunable": int(audit.get("exact_duplicate_prunable", 0)),
        "no_embedding": int(audit.get("no_embedding", 0)),
        "vague_without_anchor": int(audit.get("vague_without_anchor", 0)),
        "path_or_secret_like": int(audit.get("path_or_secret_like", 0)),
        "missing_source_interaction": int(audit.get("missing_source_interaction", 0)),
        "missing_fts": int(audit.get("missing_fts", 0)),
        "orphan_fts": int(audit.get("orphan_fts", 0)),
        "embedding_percent": _percent(lessons - int(audit.get("no_embedding", 0)), lessons),
    }
    good_interactions = outcomes["good_outcome_interactions"]
    report = {
        "interactions": interactions,
        **outcomes,
        **lesson_outcomes,
        "outcome_coverage_percent": _percent(
            outcomes["outcome_interactions"], interactions
        ),
        "lessons": lessons,
        "facts": facts,
        "grounded_lessons": grounded_lessons,
        "synthetic_lessons": lessons - grounded_lessons,
        "lesson_sources": sources,
        "lessons_per_interaction": round(lessons / interactions, 3)
        if interactions
        else 0.0,
        "distillation_yield": round(grounded_lessons / good_interactions, 3)
        if good_interactions
        else None,
        "quality": quality,
    }
    report["status"] = _status(report)
    return report


def format_report(report: dict) -> str:
    """Render a compact CLI/MCP view of ``build_report``."""
    quality = report.get("quality") or {}
    yield_value = report.get("distillation_yield")
    lines = [
        "sonder learning health",
        "  status: %s" % report.get("status", "unknown"),
        "  interactions: %s | outcome coverage: %s%% (%s grounded)"
        % (
            report.get("interactions", 0),
            report.get("outcome_coverage_percent", 0),
            report.get("outcome_interactions", 0),
        ),
        "  outcomes: %s | positive: %s%% | negative: %s"
        % (
            report.get("outcomes", 0),
            report.get("positive_percent", 0),
            report.get("bad_outcomes", 0),
        ),
        "  lessons: %s | interaction-grounded: %s | synthetic: %s"
        % (
            report.get("lessons", 0),
            report.get("grounded_lessons", 0),
            report.get("synthetic_lessons", 0),
        ),
        "  lesson feedback: evaluated=%s | with losses=%s | loss-only=%s | quarantined=%s"
        % (
            report.get("evaluated_lessons", 0),
            report.get("lessons_with_losses", 0),
            report.get("loss_only_lessons", 0),
            report.get("quarantined_lessons", 0),
        ),
        "  distillation yield: %s grounded lesson(s) per positive interaction"
        % ("n/a" if yield_value is None else yield_value),
        "  embeddings: %s%% | duplicate rows: %s | vague: %s | privacy flags: %s"
        % (
            quality.get("embedding_percent", 0),
            quality.get("exact_duplicate_prunable", 0),
            quality.get("vague_without_anchor", 0),
            quality.get("path_or_secret_like", 0),
        ),
    ]
    for item in report.get("quarantined_lesson_details") or []:
        lines.append(
            "    quarantine %s: losses=%s | tasks=%s | avg=%s | retry after=%s"
            % (
                item.get("lesson_id", "unknown"),
                item.get("losses_since_win", 0),
                item.get("distinct_loss_tasks_since_win", 0),
                item.get("avg_reward_since_win"),
                item.get("retry_after") or "manual review",
            )
        )
    sources = report.get("lesson_sources") or {}
    lines.append(
        "  lesson sources: %s"
        % (
            ", ".join("%s=%s" % item for item in sorted(sources.items()))
            if sources
            else "(none yet)"
        )
    )
    signals = report.get("signals") or []
    lines.append(
        "  signals: %s"
        % (
            ", ".join(
                "%s=%s" % (item.get("signal"), item.get("count", 0))
                for item in signals
            )
            if signals
            else "(none yet)"
        )
    )
    return "\n".join(lines)
