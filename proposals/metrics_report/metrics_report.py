"""Read-only observability over the sonder learning loop's memory_store.

Answers three questions the raw tables don't answer directly:

  1. Where did each stored lesson come from? (grounded from a real
     interaction, or a synthetic batch tagged like "seed:curriculum:..." /
     "community")
  2. How do outcome signals break down, and how much of that is "good"
     (reward.is_good) vs not?
  3. How efficiently does the loop turn activity into lessons — lessons per
     interaction, and lessons per *good-outcome* interaction (the
     "distillation yield": reflection.maybe_add_lesson silently drops vague
     or near-duplicate text, so this ratio surfaces how much of that
     filtering is happening without reading logs).

Pure stdlib. Takes an already-open memory_store connection (sqlite3) — no
model, no network, no filesystem beyond what the caller already opened.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import reward  # noqa: E402  (repo root module, read-only import)


def lesson_source_prefix(source_interaction, is_grounded):
    """Classify one lesson's `source_interaction` value into a display bucket.

    - `is_grounded` (source_interaction matches a real interactions.id) -> "interaction"
    - a synthetic tag with a colon, e.g. "seed:curriculum:strings:basic" -> "seed"
      (the segment before the first colon)
    - a synthetic tag with no colon, e.g. "community" -> the tag itself
    - missing/empty -> "unknown"
    """
    if is_grounded:
        return "interaction"
    if not source_interaction:
        return "unknown"
    if ":" in source_interaction:
        return source_interaction.split(":", 1)[0]
    return source_interaction


def lesson_source_breakdown(conn):
    """{prefix: count} across all stored lessons. See lesson_source_prefix."""
    rows = conn.execute(
        "SELECT l.source_interaction AS src, "
        "       CASE WHEN i.id IS NOT NULL THEN 1 ELSE 0 END AS grounded "
        "FROM lessons l LEFT JOIN interactions i ON i.id = l.source_interaction"
    ).fetchall()
    breakdown = {}
    for row in rows:
        prefix = lesson_source_prefix(row["src"], bool(row["grounded"]))
        breakdown[prefix] = breakdown.get(prefix, 0) + 1
    return breakdown


def outcome_signal_distribution(conn):
    """Per-signal {count, avg_reward} plus totals, using stored outcomes.reward.

    'good' classification uses reward.is_good(signal) (the same threshold the
    server applies before triggering distillation), not a re-derived cutoff.
    """
    rows = conn.execute(
        "SELECT signal, COUNT(*) AS n, AVG(reward) AS avg_reward "
        "FROM outcomes GROUP BY signal"
    ).fetchall()
    by_signal = {}
    total = 0
    good_total = 0
    for row in rows:
        sig, n, avg_r = row["signal"], row["n"], row["avg_reward"]
        by_signal[sig] = {"count": n, "avg_reward": avg_r}
        total += n
        if reward.is_good(sig):
            good_total += n
    return {
        "by_signal": by_signal,
        "total": total,
        "good_total": good_total,
        "good_fraction": (good_total / total) if total else 0.0,
    }


def _good_outcome_interaction_count(conn):
    good_signals = [s for s in reward.VALID_SIGNALS if reward.is_good(s)]
    if not good_signals:
        return 0
    placeholders = ",".join("?" * len(good_signals))
    row = conn.execute(
        "SELECT COUNT(DISTINCT interaction_id) FROM outcomes WHERE signal IN (%s)"
        % placeholders,
        tuple(good_signals),
    ).fetchone()
    return row[0]


def lessons_per_interaction(conn):
    """Yield-rate view: how much activity turns into stored lessons.

    - lessons_per_interaction: n_lessons / n_interactions (0.0 if no interactions)
    - distillation_yield: n_lessons / n_good_outcome_interactions, or None if there
      are no good-outcome interactions yet (undefined, not zero — distinguishes
      "nothing to distill from" from "distilled nothing from good outcomes").
    """
    n_interactions = conn.execute("SELECT COUNT(*) FROM interactions").fetchone()[0]
    n_lessons = conn.execute("SELECT COUNT(*) FROM lessons").fetchone()[0]
    n_good = _good_outcome_interaction_count(conn)
    return {
        "n_interactions": n_interactions,
        "n_lessons": n_lessons,
        "n_good_outcome_interactions": n_good,
        "lessons_per_interaction": (n_lessons / n_interactions) if n_interactions else 0.0,
        "distillation_yield": (n_lessons / n_good) if n_good else None,
    }


def build_report(conn):
    """Assemble the full metrics dict from a memory_store connection."""
    yield_stats = lessons_per_interaction(conn)
    return {
        "n_interactions": yield_stats["n_interactions"],
        "n_lessons": yield_stats["n_lessons"],
        "n_good_outcome_interactions": yield_stats["n_good_outcome_interactions"],
        "lessons_per_interaction": yield_stats["lessons_per_interaction"],
        "distillation_yield": yield_stats["distillation_yield"],
        "lesson_sources": lesson_source_breakdown(conn),
        "outcome_signals": outcome_signal_distribution(conn),
    }


def format_report(report):
    """Render build_report()'s dict as the multi-line text sonder_stats uses."""
    lines = ["sonder metrics report"]
    lines.append(
        "  interactions: %d | lessons: %d | good-outcome interactions: %d"
        % (
            report["n_interactions"],
            report["n_lessons"],
            report["n_good_outcome_interactions"],
        )
    )
    lines.append(
        "  lessons/interaction: %.3f" % report["lessons_per_interaction"]
    )
    dy = report["distillation_yield"]
    lines.append(
        "  distillation yield (lessons/good-outcome): %s"
        % ("%.3f" % dy if dy is not None else "n/a (no good outcomes yet)")
    )
    sources = report["lesson_sources"]
    lines.append("  lesson sources:")
    if sources:
        for prefix in sorted(sources):
            lines.append("    - %-12s %d" % (prefix, sources[prefix]))
    else:
        lines.append("    (none yet)")
    sig = report["outcome_signals"]
    lines.append(
        "  outcome signals (total=%d, good=%d, good_fraction=%.2f):"
        % (sig["total"], sig["good_total"], sig["good_fraction"])
    )
    if sig["by_signal"]:
        for name in sorted(sig["by_signal"]):
            info = sig["by_signal"][name]
            lines.append(
                "    - %-14s n=%-4d avg_reward=%+.2f" % (name, info["count"], info["avg_reward"])
            )
    else:
        lines.append("    (none yet)")
    return "\n".join(lines)
