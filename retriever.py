"""Hybrid lexical+semantic retrieval over distilled lessons. RRF fusion."""
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


def _semantic_rank(conn, qv, limit=10):
    scored = []
    for les in memory_store.all_lessons(conn):
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
    return _semantic_rank(conn, qv, limit=limit)


def _relevant_ids(conn, qv, ids, min_sim):
    """Filter fused candidate ids to those whose stored embedding clears min_sim.

    Lessons with no stored embedding are dropped (relevance can't be judged).
    """
    kept = []
    for lid in ids:
        row = conn.execute(
            "SELECT embedding FROM lessons WHERE id=?", (lid,)
        ).fetchone()
        emb = row[0] if row else None
        if not emb:
            continue
        v = embeddings.from_blob(emb)
        if embeddings.cosine(qv, v) >= min_sim:
            kept.append(lid)
    return kept


def retrieve(conn, task, k=5, embed_fn=embeddings.embed, min_sim=None):
    rows = retrieve_with_ids(conn, task, k=k, embed_fn=embed_fn, min_sim=min_sim)
    return [r["text"] for r in rows]


def _usage_boost(conn, lesson_id):
    stats = memory_store.lesson_usage_stats(conn).get(lesson_id)
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
        min_sim = float(os.environ.get("TRILOBITE_MIN_SIM", str(DEFAULT_MIN_SIM)))

    lexical = memory_store.fts_search(conn, task, limit=10)
    qv = embed_fn(task)

    if qv is None:
        # Embeddings unavailable: soft-fail to lexical-only, no threshold possible.
        scores = rrf_scores([lexical, []])
        fused = sorted(scores, key=lambda lid: -(scores[lid] + _usage_boost(conn, lid)))[:k]
        rows = []
        for lid in fused:
            text = memory_store.get_lesson_text(conn, lid)
            if text:
                rows.append({"id": lid, "text": text, "score": scores[lid]})
        return rows

    semantic = _semantic_rank(conn, qv, limit=10)
    scores = rrf_scores([lexical, semantic])
    fused = sorted(scores, key=lambda lid: -(scores[lid] + _usage_boost(conn, lid)))
    relevant = _relevant_ids(conn, qv, fused, min_sim)[:k]
    rows = []
    for lid in relevant:
        text = memory_store.get_lesson_text(conn, lid)
        if text:
            rows.append({"id": lid, "text": text, "score": scores.get(lid, 0.0)})
    return rows
