"""Read-only memory quality audits plus conservative duplicate cleanup."""
import collections
import re

import contribute
import memory_store

LONG_LESSON_CHARS = 220

_VAGUE_MARKERS = re.compile(
    r"\b(use appropriate|be careful|handle errors|write clean|ensure proper|"
    r"best practices|make sure|properly)\b",
    re.I,
)
_CONCRETE_ANCHOR = re.compile(
    r"`[^`]+`|\b\w+\.\w+|\b\w+_\w+|[A-Za-z]+[A-Z][a-z]|O\([^)]*\)"
)
_INTERACTION_ID = re.compile(r"^[0-9a-f]{16}$", re.I)


def normalize_lesson_text(text):
    """Canonical text for exact duplicate detection."""
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _has_anchor(text):
    return bool(_CONCRETE_ANCHOR.search(text or ""))


def _all_lessons(conn):
    rows = conn.execute(
        "SELECT id, text, source_interaction, ts, length(coalesce(text,'')) AS n, "
        "embedding IS NOT NULL AS has_embedding "
        "FROM lessons ORDER BY ts ASC, rowid ASC"
    ).fetchall()
    return [dict(r) for r in rows]


def _usage_stats(conn):
    return memory_store.lesson_usage_stats(conn)


def choose_exact_duplicate_keeper(group, stats=None):
    """Pick the survivor in an exact-text duplicate group.

    Prefer proven lessons first, then more-used lessons, then longer/more detailed
    text, then the oldest row. Exact duplicates have the same text, but this
    keeps the rule robust if whitespace/case differ.
    """
    stats = stats or {}
    return sorted(
        group,
        key=lambda row: (
            -float(stats.get(row["id"], {}).get("avg_reward") or -2.0),
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
        and _INTERACTION_ID.match(str(r["source_interaction"]))
        and not conn.execute(
            "SELECT 1 FROM interactions WHERE id=?", (r["source_interaction"],)
        ).fetchone()
    ]
    return {
        "total_lessons": len(lessons),
        "exact_duplicate_groups": len(exact_plan),
        "exact_duplicate_prunable": sum(len(e["prune_ids"]) for e in exact_plan),
        "no_embedding": len(no_embedding),
        "long_over_%d" % LONG_LESSON_CHARS: len(long_rows),
        "vague_without_anchor": len(vague),
        "path_or_secret_like": len(path_or_secret),
        "no_terminal_punctuation": len(no_punctuation),
        "missing_source_interaction": len(source_missing),
        "missing_fts": len(missing_fts),
        "orphan_fts": len(orphan_fts),
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
