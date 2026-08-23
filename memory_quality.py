"""Read-only memory quality audits plus conservative duplicate cleanup."""
import collections
import re
from datetime import datetime, timezone

import contribute
import lesson_decay
import lesson_pruner
import sonder_runtime.adapters.memory_store as memory_store
from sonder_runtime.domain.memory import rules as memory_rules

LONG_LESSON_CHARS = 220

# A stale finding claims a lesson's POSITIVE evidence has aged out, so both
# gates are about the evidence, not the text: the newest scored outcome must be
# at least two half-lives old, and the age-decayed effective score must have
# fallen below this floor. Diagnostics only -- nothing reads these to change
# retrieval -- and experimental until the thresholds have been measured against
# the live corpus the way retriever's quarantine bands were.
STALE_MIN_AGE_DAYS = 2 * lesson_decay.DEFAULT_HALF_LIFE_DAYS
STALE_EFFECTIVE_FLOOR = 0.2

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


def _evidence_polarity_score(stats):
    """(mean_reward, population) from scored outcomes, caller-judged first.

    The deciding population is the caller's wherever a caller has judged the
    lesson; the runtime's own execution grades only speak when nobody reviewed
    it -- the same doctrine retriever._usage_boost and
    memory_rules.retention_rank already apply, restated here rather than
    re-invented. Returns ``(None, "")`` when the lesson has no scored evidence
    at all: no provenance, no claim.
    """
    if not stats:
        return None, ""
    mean = stats.get("avg_reward_caller")
    if mean is not None and int(stats.get("scored_caller") or 0) > 0:
        return float(mean), "caller"
    mean = stats.get("avg_reward_execution")
    if mean is not None and int(stats.get("scored_execution") or 0) > 0:
        return float(mean), "execution"
    return None, ""


def contradiction_findings(conn, sim_threshold=None, limit=20):
    """Similar lessons whose grounded outcomes disagree. Read-only.

    Wires ``lesson_decay.detect_contradictions`` (pure logic that previously
    had no production caller) to the store: similarity comes from stored
    embeddings compared only within one exact (model, revision, dimension)
    space, and polarity comes from scored outcome evidence via
    ``_evidence_polarity_score``. Everything a claim rests on is evidence the
    store actually holds -- a lesson with no usable embedding, or no graded
    outcome, is excluded rather than guessed about, so a directive that merely
    SOUNDS negative can never be flagged against one that sounds positive.
    """
    limit = max(1, min(int(limit or 20), 100))
    threshold = float(
        lesson_decay.DEFAULT_SIM_THRESHOLD if sim_threshold is None else sim_threshold
    )
    stats = _usage_stats(conn)
    findings = []
    for space_lessons in lesson_pruner.embedded_lessons_by_space(conn).values():
        candidates = []
        unit_by_text = {}
        seen_texts = set()
        for lesson in space_lessons:
            mean, population = _evidence_polarity_score(stats.get(lesson["id"]))
            if mean is None or mean == 0.0:
                continue  # neutral or unmeasured: polarity cannot be claimed
            text = lesson.get("text") or ""
            # detect_contradictions keys its similarity callback by text, so
            # exact-duplicate texts would collide in unit_by_text. They are
            # the exact-duplicate audit's finding, not a contradiction -- one
            # statement cannot disagree with itself -- so keep the first copy.
            normalized = normalize_lesson_text(text)
            if normalized in seen_texts:
                continue
            seen_texts.add(normalized)
            candidates.append({
                "id": lesson["id"], "text": text, "score": mean,
                "population": population, "ts": lesson.get("ts"),
            })
            # Normalize once so pairwise similarity is a plain dot product.
            # Cosine is scale-invariant, so this changes nothing but the cost:
            # the pair loop below is O(opposite pairs) similarity calls, and
            # recomputing both norms inside every call is what would make this
            # audit expensive on the live corpus. _load_lessons has already
            # excluded zero-norm vectors, so the division is safe.
            vector = lesson["vector"]
            norm = sum(x * x for x in vector) ** 0.5
            unit_by_text[text] = [x / norm for x in vector]
        if len(candidates) < 2:
            continue
        conflicts = lesson_decay.detect_contradictions(
            candidates,
            lambda a, b: sum(
                x * y for x, y in zip(unit_by_text[a], unit_by_text[b])
            ),
            sim_threshold=threshold,
        )
        for conflict in conflicts:
            a, b = conflict["a"], conflict["b"]
            findings.append({
                "a_id": a["id"],
                "b_id": b["id"],
                "similarity": round(float(conflict["similarity"]), 4),
                "a_evidence": {
                    "mean_reward": round(a["score"], 3),
                    "population": a["population"],
                },
                "b_evidence": {
                    "mean_reward": round(b["score"], 3),
                    "population": b["population"],
                },
                "a_preview": _truncate(a["text"]),
                "b_preview": _truncate(b["text"]),
            })
    findings.sort(key=lambda f: (-f["similarity"], f["a_id"], f["b_id"]))
    return findings[:limit]


def _parse_evidence_ts(value):
    """Aware UTC datetime from a stored timestamp, or None when unreadable."""
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def stale_lesson_findings(
    conn, now=None, half_life_days=lesson_decay.DEFAULT_HALF_LIFE_DAYS, limit=20,
):
    """Lessons whose positive evidence has aged out. Read-only, experimental.

    Wires ``lesson_decay.effective_score`` to the store as a diagnostic: a
    lesson whose evidence mean is positive, whose newest scored outcome is at
    least ``STALE_MIN_AGE_DAYS`` old, and whose age-decayed effective score has
    fallen below ``STALE_EFFECTIVE_FLOOR`` is reported as stale. Nothing here
    changes retrieval -- quarantine handles measured harm, and unvalidated
    lessons are already counted by the audit; this names the third population,
    lessons whose proof of usefulness has simply gone old. Fails closed the
    same way the contradiction audit does: no scored evidence, or an
    unreadable evidence timestamp, and no claim is made. ``now`` is injectable
    so callers and tests are deterministic.
    """
    limit = max(1, min(int(limit or 20), 100))
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    history = memory_store.lesson_usage_history(conn)
    stats = memory_store.lesson_usage_stats(conn, history=history)
    last_evidence = {}
    for row in history:
        # History is ordered by lesson then evidence time, so the last row
        # seen per lesson is its newest scored outcome.
        last_evidence[row["lesson_id"]] = row["evidence_ts"]
    texts = {row["id"]: row.get("text") or "" for row in _all_lessons(conn)}
    findings = []
    for lesson_id, lesson_stats in stats.items():
        if lesson_id not in texts:
            continue  # usage rows can outlive a deleted lesson
        mean, population = _evidence_polarity_score(lesson_stats)
        if mean is None or mean <= 0.0:
            continue  # harm is quarantine's question, absence is unvalidated's
        newest = _parse_evidence_ts(last_evidence.get(lesson_id))
        if newest is None:
            continue
        age_days = max(0.0, (current - newest).total_seconds() / 86400.0)
        if age_days < STALE_MIN_AGE_DAYS:
            continue
        wins = int(lesson_stats.get("wins") or 0)
        scored = wins + int(lesson_stats.get("losses") or 0)
        effective = lesson_decay.effective_score(
            mean, age_days, uses=scored, hits=wins, half_life_days=half_life_days,
        )
        if effective >= STALE_EFFECTIVE_FLOOR:
            continue
        findings.append({
            "id": lesson_id,
            "preview": _truncate(texts[lesson_id]),
            "age_days": round(age_days, 1),
            "effective_score": round(effective, 4),
            "mean_reward": round(mean, 3),
            "population": population,
            "last_evidence_ts": last_evidence.get(lesson_id),
        })
    findings.sort(key=lambda f: (-f["age_days"], f["id"]))
    return findings[:limit]


def duplicate_fact_findings(conn):
    """Exact-duplicate facts within each project scope. Read-only.

    Facts are asserted context injected into every project-scoped prompt, so a
    duplicate spends prompt budget on a repeat forever. Grouping is by
    (project, normalized text): the same statement asserted in two different
    projects is NOT a duplicate -- project scope is a privacy boundary, and a
    cross-scope match would reveal one project's facts while auditing another.
    No repair path is offered here on purpose: facts are directly asserted, so
    removal stays with sonder_forget_fact's explicit per-id confirmation.
    """
    rows = conn.execute(
        "SELECT id, project, text, ts FROM facts ORDER BY ts ASC, rowid ASC"
    ).fetchall()
    groups = collections.defaultdict(list)
    for raw in rows:
        row = dict(raw)
        key = memory_store.normalize_fact_text(row.get("text"))
        if key:
            groups[(row.get("project"), key)].append(row)
    findings = []
    for (project, _key), members in groups.items():
        if len(members) < 2:
            continue
        keeper = members[0]  # the oldest assertion is the original
        findings.append({
            "project": project,
            "keeper_id": keeper["id"],
            "duplicate_ids": [m["id"] for m in members[1:]],
            "preview": _truncate(keeper.get("text") or ""),
            "cluster_size": len(members),
        })
    findings.sort(key=lambda f: (-len(f["duplicate_ids"]), str(f["project"] or "")))
    return findings


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
    # Evidence-level findings: same-topic lessons whose grounded outcomes
    # disagree, positive evidence that has aged out, and repeated fact
    # assertions. All three are read-only and fail closed on missing
    # provenance (no embedding or no scored outcome means no claim).
    conflicts = contradiction_findings(conn)
    stale = stale_lesson_findings(conn)
    fact_duplicates = duplicate_fact_findings(conn)
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
        "conflicting_lesson_pairs": len(conflicts),
        "stale_lessons": len(stale),
        "duplicate_fact_groups": len(fact_duplicates),
        "duplicate_fact_rows": sum(
            len(f["duplicate_ids"]) for f in fact_duplicates
        ),
        "samples": {
            "duplicates": exact_plan[:5],
            "long": long_rows[:5],
            "vague": vague[:5],
            "path_or_secret": path_or_secret[:5],
            "missing_fts": missing_fts[:5],
            "orphan_fts": orphan_fts[:5],
            "conflicts": conflicts[:5],
            "stale": stale[:5],
            "duplicate_facts": fact_duplicates[:5],
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
        "  conflicting lesson pairs (similar text, opposite grounded "
        "outcomes): %s" % report.get("conflicting_lesson_pairs", 0),
        "  stale lessons (positive evidence aged out; experimental): %s"
        % report.get("stale_lessons", 0),
        "  duplicate facts: %s group(s), %s redundant row(s) "
        "(remove via sonder_forget_fact, never automatically)"
        % (
            report.get("duplicate_fact_groups", 0),
            report.get("duplicate_fact_rows", 0),
        ),
    ]
    conflict_rows = report.get("samples", {}).get("conflicts", [])[:sample_limit]
    if conflict_rows:
        lines.append("  conflict samples:")
        for row in conflict_rows:
            lines.append(
                "    %s (%+.2f %s) vs %s (%+.2f %s) sim=%.3f: %s"
                % (
                    row["a_id"],
                    row["a_evidence"]["mean_reward"],
                    row["a_evidence"]["population"],
                    row["b_id"],
                    row["b_evidence"]["mean_reward"],
                    row["b_evidence"]["population"],
                    row["similarity"],
                    row["a_preview"],
                )
            )
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
