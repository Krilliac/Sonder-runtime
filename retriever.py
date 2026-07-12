"""Hybrid lexical+semantic retrieval over distilled lessons. RRF fusion."""
from datetime import datetime, timedelta, timezone
import os

import embeddings
import memory_store

# Recalibrated 2026-07-06 against the 557-lesson corpus via tune_min_sim.py
# (nomic-embed-text). Over 22 natural-language coding intents vs 15 off-domain
# noise probes, top-1 cosine separated cleanly: positives min 0.612 / median
# 0.728; negatives max 0.611. 0.62 is the lowest zero-noise threshold — recall
# 0.95, noise 0.00 (best Youden's J). The old 0.65, tuned on the tiny
# game-ladder corpus, dropped genuine 0.60-0.65 hits (e.g. the sql-injection
# lesson at 0.650) with no precision gain. Re-run tune_min_sim.py after large
# corpus changes.
DEFAULT_MIN_SIM = 0.62
QUARANTINE_MIN_LOSSES = 5
QUARANTINE_REPEAT_TASK_MIN_LOSSES = 6
QUARANTINE_MIN_DISTINCT_TASKS = 2
QUARANTINE_MAX_AVG_REWARD = -0.5
QUARANTINE_COOLDOWN_HOURS = 24 * 7


def rrf(rank_lists, k=60):
    scores = {}
    for lst in rank_lists:
        for rank, item in enumerate(lst):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores, key=lambda i: -scores[i])


def rrf_scores(rank_lists, k=60):
    scores = {}
    for lst in rank_lists:
        for rank, item in enumerate(lst):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank + 1)
    return scores


def _semantic_rank(conn, qv, limit=10, exclude_ids=None):
    excluded = set(exclude_ids or ())
    scored = []
    for les in memory_store.all_lessons(conn):
        if les["id"] in excluded:
            continue
        emb = les["embedding"]
        if not emb:
            continue
        v = embeddings.from_blob(emb)
        scored.append((embeddings.cosine(qv, v), les["id"]))
    scored.sort(reverse=True)
    return [lid for _, lid in scored[:limit]]


def semantic_search(conn, task, embed_fn=embeddings.embed, limit=10):
    qv = embed_fn(task)
    if qv is None:
        return []
    usage_stats = memory_store.lesson_usage_stats(conn)
    quarantined = {
        lesson_id for lesson_id, stats in usage_stats.items()
        if _lesson_quarantined(stats)
    }
    return _semantic_rank(conn, qv, limit=limit, exclude_ids=quarantined)


def _lesson_data(conn, ids):
    """Load candidate text and embeddings in bounded batches, preserving IDs."""
    unique_ids = list(dict.fromkeys(ids))
    rows = {}
    for offset in range(0, len(unique_ids), 900):
        batch = unique_ids[offset:offset + 900]
        if not batch:
            continue
        placeholders = ",".join("?" for _ in batch)
        for row in conn.execute(
            "SELECT id, text, embedding FROM lessons WHERE id IN (%s)"
            % placeholders,
            tuple(batch),
        ).fetchall():
            rows[row["id"]] = row
    return rows


def _relevant_ids(conn, qv, ids, min_sim, lesson_data=None):
    """Filter fused candidate ids to those whose stored embedding clears min_sim.

    Lessons with no stored embedding are dropped (relevance can't be judged).
    """
    data = lesson_data if lesson_data is not None else _lesson_data(conn, ids)
    kept = []
    for lid in ids:
        row = data.get(lid)
        emb = row["embedding"] if row else None
        if not emb:
            continue
        v = embeddings.from_blob(emb)
        if embeddings.cosine(qv, v) >= min_sim:
            kept.append(lid)
    return kept


def retrieve(conn, task, k=5, embed_fn=embeddings.embed, min_sim=None):
    rows = retrieve_with_ids(conn, task, k=k, embed_fn=embed_fn, min_sim=min_sim)
    return [r["text"] for r in rows]


def lesson_quarantine(stats, now=None):
    """Return the active quarantine decision and its auditable evidence.

    Evidence is scoped to the epoch since the last grounded success so a lesson
    can recover and can also relapse. Five losses must span at least two task
    formulations; six identical-task failures are also accepted as strong
    repeat evidence. Quarantine expires into automatic probation after a week,
    giving the lesson a production path to earn a new positive outcome.
    """
    if not stats:
        return {"active": False}
    losses = int(stats.get("losses_since_win") or 0)
    distinct_tasks = int(stats.get("distinct_loss_tasks_since_win") or 0)
    avg = stats.get("avg_reward_since_win")
    enough_evidence = losses >= QUARANTINE_MIN_LOSSES and (
        distinct_tasks >= QUARANTINE_MIN_DISTINCT_TASKS
        or losses >= QUARANTINE_REPEAT_TASK_MIN_LOSSES
    )
    threshold_met = bool(
        enough_evidence
        and avg is not None
        and float(avg) <= QUARANTINE_MAX_AVG_REWARD
    )
    retry_after = None
    last_failure = stats.get("last_failure_ts")
    if threshold_met and last_failure:
        try:
            failed_at = datetime.fromisoformat(str(last_failure).replace("Z", "+00:00"))
            if failed_at.tzinfo is None:
                failed_at = failed_at.replace(tzinfo=timezone.utc)
            retry_at = failed_at + timedelta(hours=QUARANTINE_COOLDOWN_HOURS)
            retry_after = retry_at.isoformat()
            current = now or datetime.now(timezone.utc)
            if current.tzinfo is None:
                current = current.replace(tzinfo=timezone.utc)
            threshold_met = current < retry_at
        except (TypeError, ValueError):
            # Malformed evidence timestamps fail safe and stay visible in health.
            retry_after = None
    return {
        "active": threshold_met,
        "losses_since_win": losses,
        "distinct_loss_tasks_since_win": distinct_tasks,
        "avg_reward_since_win": avg,
        "last_failure_ts": last_failure,
        "retry_after": retry_after,
    }


def _lesson_quarantined(stats, now=None):
    return bool(lesson_quarantine(stats, now=now).get("active"))


def _usage_boost(stats):
    if not stats:
        return 0.0
    uses = stats.get("uses") or 0
    avg = stats.get("avg_reward")
    if avg is None:
        return 0.0
    # Keep historical outcome as a gentle tiebreaker, not a relevance override.
    return max(-0.01, min(0.01, float(avg) * min(uses, 10) / 1000.0))


def retrieve_with_ids(conn, task, k=5, embed_fn=embeddings.embed, min_sim=None):
    if min_sim is None:
        min_sim = float(os.environ.get("SONDER_MIN_SIM", str(DEFAULT_MIN_SIM)))

    usage_stats = memory_store.lesson_usage_stats(conn)
    quarantined = {
        lesson_id for lesson_id, stats in usage_stats.items()
        if _lesson_quarantined(stats)
    }
    candidate_limit = max(10, int(k) * 4)
    # Over-fetch enough lexical hits that filtering cannot starve valid matches
    # behind quarantined results ranked ahead of them by FTS.
    lexical_limit = candidate_limit + len(quarantined)
    lexical = [
        lesson_id
        for lesson_id in memory_store.fts_search(
            conn, task, limit=lexical_limit,
        )
        if lesson_id not in quarantined
    ]
    qv = embed_fn(task)

    if qv is None:
        # Embeddings unavailable: soft-fail to lexical-only, no threshold possible.
        scores = rrf_scores([lexical, []])
        fused = sorted(
            scores,
            key=lambda lid: -(scores[lid] + _usage_boost(usage_stats.get(lid))),
        )[:k]
        data = _lesson_data(conn, fused)
        rows = []
        for lid in fused:
            row = data.get(lid)
            text = row["text"] if row else None
            if text:
                rows.append({"id": lid, "text": text, "score": scores[lid]})
        return rows

    semantic = _semantic_rank(
        conn, qv, limit=candidate_limit, exclude_ids=quarantined,
    )
    scores = rrf_scores([lexical, semantic])
    fused = sorted(
        scores,
        key=lambda lid: -(scores[lid] + _usage_boost(usage_stats.get(lid))),
    )
    data = _lesson_data(conn, fused)
    relevant = _relevant_ids(
        conn, qv, fused, min_sim, lesson_data=data,
    )[:k]
    rows = []
    for lid in relevant:
        row = data.get(lid)
        text = row["text"] if row else None
        if text:
            rows.append({"id": lid, "text": text, "score": scores.get(lid, 0.0)})
    return rows
