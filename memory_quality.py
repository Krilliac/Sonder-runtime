"""Read-only memory quality audits plus conservative duplicate cleanup."""
import collections
import re

import contribute
import sonder_runtime.adapters.memory_store as memory_store
from sonder_runtime.domain.memory import rules as memory_rules

LONG_LESSON_CHARS = 220

_VAGUE_MARKERS = re.compile(
    r"\b(use appropriate|be careful|handle errors|write clean|ensure proper|"
    r"best practices|make sure|properly)\b",
    re.I,
)
_CONCRETE_ANCHOR = re.compile(
    r"`[^`]+`|\b\w+\.\w+|\b\w+_\w+|[A-Za-z]+[A-Z][a-z]|O\([^)]*\)"
)
INTERACTION_ID_RE = re.compile(r"^[0-9a-f]{16}$", re.I)


def normalize_lesson_text(text):
    """Canonical text for exact duplicate detection."""
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _has_anchor(text):
    return bool(_CONCRETE_ANCHOR.search(text or ""))


def _all_lessons(conn):
    rows = conn.execute(
        "SELECT l.id, l.text, l.source_interaction, l.ts, "
        "length(coalesce(l.text,'')) AS n, "
        "l.embedding IS NOT NULL AS has_embedding, "
        "CASE WHEN i.id IS NULL THEN 0 ELSE 1 END AS grounded "
        "FROM lessons l LEFT JOIN interactions i ON i.id=l.source_interaction "
        "ORDER BY l.ts ASC, l.rowid ASC"
    ).fetchall()
    return [dict(r) for r in rows]


def _graded_lesson_ids(conn):
    """IDs of lessons that have at least one retrieval with a scored outcome.

    A lesson can be retrieved and never graded (the caller never recorded an
    outcome), so this is deliberately narrower than "appears in lesson_usage".
    """
    return {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT lesson_id FROM lesson_usage WHERE reward IS NOT NULL"
        ).fetchall()
    }


def _usage_stats(conn):
    return memory_store.lesson_usage_stats(conn)


def choose_exact_duplicate_keeper(group, stats=None):
    """Pick the survivor in an exact-text duplicate group.

    Prefer proven lessons first, then more-used lessons, then longer/more detailed
    text, then the oldest row. Exact duplicates have the same text, but this
    keeps the rule robust if whitespace/case differ.

    The question this asks is RETENTION -- whose judgement would be lost if
    this row were deleted -- and the rule for it is written down once, in
    ``memory_rules.retention_rank``. It is deliberately NOT the credit rule
    (``memory_rules.evidence_rank``), which conditions population on
    eligibility; see the "two questions" note beside them. This module used to
    hold a private closure named ``evidence_rank`` that meant the opposite of
    the domain function of the same name, which is precisely how two sites come
    to teach contradictory rules for one concept without anyone noticing.

    Evidence is read as TWO ranks, never one average. ``avg_reward`` is a mean
    over both outcome populations, so ranking on it kept the lesson the runtime
    graded 1.0 by running its own tests over one a caller reviewed and accepted
    at 0.8 -- and the reviewed copy was the row deleted.
    """
    stats = stats or {}

    def retention(row):
        row_stats = stats.get(row["id"], {})
        return memory_rules.retention_rank(
            row_stats.get("avg_reward_caller"),
            row_stats.get("avg_reward_execution"),
        )

    return sorted(
        group,
        key=lambda row: (
            *retention(row),
            -int(stats.get(row["id"], {}).get("wins") or 0),
            -int(stats.get(row["id"], {}).get("uses") or 0),
            -len(row.get("text") or ""),
            row.get("ts") or "",
        ),
    )[0]


def exact_duplicate_plan(conn):
    """Build a no-mutation plan for deleting exact duplicate lesson rows."""
    groups = collections.defaultdict(list)
    for row in _all_lessons(conn):
        key = normalize_lesson_text(row.get("text"))
        if key:
            groups[key].append(row)

    stats = _usage_stats(conn)
    plan = []
    for rows in groups.values():
        if len(rows) < 2:
            continue
        keeper = choose_exact_duplicate_keeper(rows, stats)
        losers = [r for r in rows if r["id"] != keeper["id"]]
        plan.append({
            "keeper_id": keeper["id"],
            "keeper_text": keeper.get("text") or "",
            "prune_ids": [r["id"] for r in losers],
            "prune_texts": [r.get("text") or "" for r in losers],
            "cluster_size": len(rows),
        })
    plan.sort(key=lambda item: (-len(item["prune_ids"]), item["keeper_text"].lower()))
    return plan


def apply_exact_duplicate_plan(conn, plan, delete_fn=memory_store.delete_lesson):
    deleted = 0
    for entry in plan:
        for lesson_id in entry["prune_ids"]:
            if delete_fn(conn, lesson_id):
                deleted += 1
    return deleted


def audit(conn):
    """Return structured quality counters and small samples."""
    lessons = _all_lessons(conn)
    exact_plan = exact_duplicate_plan(conn)
    missing_fts = []
    for row in lessons:
        if not conn.execute(
            "SELECT 1 FROM lessons_fts WHERE lesson_id=?", (row["id"],)
        ).fetchone():
            missing_fts.append(row)
    # Count and sample separately. len() of a LIMIT-20 slice was published as
    # the orphan counter, on the same output line as missing_fts, which is an
    # uncapped full scan -- so a store with 5000 dangling FTS rows reported
    # "orphan=20" beside an honest number and read as a small bounded problem.
    orphan_fts_total = conn.execute(
        "SELECT COUNT(*) FROM lessons_fts "
        "WHERE lesson_id NOT IN (SELECT id FROM lessons)"
    ).fetchone()[0]
    orphan_fts = [dict(r) for r in conn.execute(
        "SELECT lesson_id, text FROM lessons_fts "
        "WHERE lesson_id NOT IN (SELECT id FROM lessons) LIMIT 20"
    ).fetchall()]
    long_rows = [r for r in lessons if int(r.get("n") or 0) > LONG_LESSON_CHARS]
    no_embedding = [r for r in lessons if not r.get("has_embedding")]
    path_or_secret = []
    for row in lessons:
        reasons = contribute.private_reasons(row.get("text") or "")
        if not reasons:
            continue
        path_or_secret.append({
            "id": row["id"],
            "source_interaction": row.get("source_interaction"),
            "ts": row.get("ts"),
            "n": row.get("n", 0),
            "has_embedding": row.get("has_embedding", False),
            "privacy_reasons": reasons,
            "privacy_preview": contribute.privacy_preview(row.get("text") or ""),
        })
    vague = [
        r for r in lessons
        if _VAGUE_MARKERS.search(r.get("text") or "") and not _has_anchor(r.get("text") or "")
    ]
    no_punctuation = [
        r for r in lessons
        if (r.get("text") or "").strip()
        and (r.get("text") or "").strip()[-1:] not in ".!?`"
    ]
    source_missing = [
        r for r in lessons
        if r.get("source_interaction")
        and INTERACTION_ID_RE.match(str(r["source_interaction"]))
        and not conn.execute(
            "SELECT 1 FROM interactions WHERE id=?", (r["source_interaction"],)
        ).fetchone()
    ]
    # A lesson can be wrong in two ways this audit could not previously see:
    # it was never grounded in a real interaction (seeded text nobody earned),
    # or it was never retrieved on a task that produced a scored outcome. Both
    # populations are invisible in every text-hygiene counter above -- a seed
    # lesson with a code anchor and a terminal period looks perfect. On the
    # live store that is 528 ungrounded and 715 never-graded of 1061 lessons,
    # so a clean hygiene report was describing a third of the corpus.
    graded_ids = _graded_lesson_ids(conn)
    grounded = [r for r in lessons if r.get("grounded")]
    synthetic = [r for r in lessons if not r.get("grounded")]
    unvalidated = [r for r in lessons if r["id"] not in graded_ids]
    synthetic_unvalidated = [r for r in synthetic if r["id"] not in graded_ids]
    return {
        "total_lessons": len(lessons),
        "grounded_lessons": len(grounded),
        "synthetic_lessons": len(synthetic),
        "unvalidated_lessons": len(unvalidated),
        "synthetic_unvalidated_lessons": len(synthetic_unvalidated),
        "exact_duplicate_groups": len(exact_plan),
        "exact_duplicate_prunable": sum(len(e["prune_ids"]) for e in exact_plan),
        "no_embedding": len(no_embedding),
        "long_over_%d" % LONG_LESSON_CHARS: len(long_rows),
        "vague_without_anchor": len(vague),
        "path_or_secret_like": len(path_or_secret),
        "no_terminal_punctuation": len(no_punctuation),
        "missing_source_interaction": len(source_missing),
        "missing_fts": len(missing_fts),
        "orphan_fts": orphan_fts_total,
        "orphan_fts_sampled": len(orphan_fts),
        "samples": {
            "duplicates": exact_plan[:5],
            "long": long_rows[:5],
            "vague": vague[:5],
            "path_or_secret": path_or_secret[:5],
            "missing_fts": missing_fts[:5],
            "orphan_fts": orphan_fts[:5],
        },
    }


def _truncate(text, n=90):
    text = text or ""
    return text if len(text) <= n else text[: n - 3] + "..."


def format_audit(report, sample_limit=5):
    lines = [
        "memory quality report",
        "  lessons: %(total_lessons)s" % report,
        "  grounded in an interaction: %s | synthetic (seeded, never earned): %s"
        % (report.get("grounded_lessons", 0), report.get("synthetic_lessons", 0)),
        "  never validated by an outcome: %s (of which synthetic: %s) "
        "-- text hygiene below says nothing about these"
        % (
            report.get("unvalidated_lessons", 0),
            report.get("synthetic_unvalidated_lessons", 0),
        ),
        "  exact duplicates: %(exact_duplicate_groups)s group(s), "
        "%(exact_duplicate_prunable)s prunable row(s)" % report,
        "  no embeddings: %(no_embedding)s" % report,
        "  long lessons: %s" % report.get("long_over_%d" % LONG_LESSON_CHARS, 0),
        "  vague/no-anchor: %(vague_without_anchor)s" % report,
        "  path/secret-like: %(path_or_secret_like)s" % report,
        "  source missing: %(missing_source_interaction)s" % report,
        "  fts issues: missing=%(missing_fts)s orphan=%(orphan_fts)s" % report,
    ]
    dups = report.get("samples", {}).get("duplicates", [])[:sample_limit]
    if dups:
        lines.append("  duplicate samples:")
        for entry in dups:
            lines.append("    keep %s, prune %d: %s" % (
                entry["keeper_id"], len(entry["prune_ids"]),
                _truncate(entry["keeper_text"]),
            ))
    private_rows = report.get("samples", {}).get("path_or_secret", [])[:sample_limit]
    if private_rows:
        lines.append("  privacy review samples (redacted):")
        for row in private_rows:
            lines.append("    %s [%s]: %s" % (
                row["id"], ",".join(row.get("privacy_reasons") or []),
                row.get("privacy_preview") or "<empty>",
            ))
        lines.append("  use memory_privacy_repair with explicit lesson IDs; dry-run first.")
    return "\n".join(lines)


def repair_exact_duplicates(conn, apply=False):
    apply = apply is True
    plan = exact_duplicate_plan(conn)
    deleted = 0 if not apply else apply_exact_duplicate_plan(conn, plan)
    return plan, deleted


def privacy_findings(conn, limit=20):
    """Return bounded, redacted findings; never return the raw lesson text."""
    limit = max(1, min(int(limit or 20), 100))
    rows = conn.execute(
        "SELECT id, text, source_interaction, ts FROM lessons "
        "ORDER BY ts ASC, rowid ASC"
    ).fetchall()
    findings = []
    for raw in rows:
        row = dict(raw)
        reasons = contribute.private_reasons(row.get("text") or "")
        if not reasons:
            continue
        findings.append({
            "id": row["id"],
            "source_interaction": row.get("source_interaction"),
            "ts": row.get("ts"),
            "reasons": reasons,
            "preview": contribute.privacy_preview(row.get("text") or ""),
        })
        if len(findings) >= limit:
            break
    return findings


def privacy_cleanup_plan(conn, lesson_ids):
    """Classify explicit IDs; only currently flagged lessons are eligible."""
    requested = []
    seen = set()
    for value in lesson_ids or []:
        lesson_id = str(value or "").strip()
        if lesson_id and lesson_id not in seen:
            requested.append(lesson_id)
            seen.add(lesson_id)
    if not requested:
        return {"eligible": [], "missing": [], "not_flagged": []}
    rows = {}
    for lesson_id in requested:
        row = conn.execute(
            "SELECT id, text FROM lessons WHERE id=?", (lesson_id,)
        ).fetchone()
        if row:
            rows[lesson_id] = dict(row)
    eligible = []
    missing = []
    not_flagged = []
    for lesson_id in requested:
        row = rows.get(lesson_id)
        if row is None:
            missing.append(lesson_id)
            continue
        reasons = contribute.private_reasons(row.get("text") or "")
        if not reasons:
            not_flagged.append(lesson_id)
            continue
        eligible.append({
            "id": lesson_id,
            "reasons": reasons,
            "preview": contribute.privacy_preview(row.get("text") or ""),
        })
    return {
        "eligible": eligible,
        "missing": missing,
        "not_flagged": not_flagged,
    }


def apply_privacy_cleanup(conn, plan, delete_fn=memory_store.delete_lesson):
    deleted = 0
    for row in plan.get("eligible", []):
        if delete_fn(conn, row["id"]):
            deleted += 1
    return deleted
