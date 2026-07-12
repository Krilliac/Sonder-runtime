"""Semantic recall of whole past solutions (not just distilled lessons).

Finds prior interactions whose *task* is semantically close to the current one AND
that ended in a good outcome, so sonder can reuse what already worked. Mirrors
retriever.py's cosine + min_sim approach, but over interactions instead of lessons.
Soft-fails to [] whenever embeddings are unavailable — never raises.
"""
import os

import embeddings
import memory_store

# Stricter than lessons' 0.65: a recall injects a whole task+solution, so we only
# want genuinely close matches. Env-overridable like SONDER_MIN_SIM.
DEFAULT_MIN_SIM = 0.72
MAX_RESP_CHARS = 400


def _format(task, response, max_len=MAX_RESP_CHARS):
    resp = response or ""
    if len(resp) > max_len:
        resp = resp[:max_len].rstrip() + " …"
    return "%s -> %s" % (task, resp)


def recall(conn, task, k=2, embed_fn=embeddings.embed, min_sim=None,
           qv=None, exclude_session=None):
    """Top-k good-outcome past interactions similar to `task`, formatted for injection.

    qv: precomputed query embedding (avoids a second embed call); if None it is
    computed from `task`. Returns [] if embeddings are down or nothing clears min_sim.
    """
    if min_sim is None:
        min_sim = float(os.environ.get("SONDER_RECALL_MIN_SIM", str(DEFAULT_MIN_SIM)))
    if qv is None:
        qv = embed_fn(task)
    if qv is None:
        return []

    scored = []
    for row in memory_store.good_interactions_with_embeddings(conn, exclude_session):
        emb = row.get("task_embedding")
        if not emb:
            continue
        sim = embeddings.cosine(qv, embeddings.from_blob(emb))
        if sim >= min_sim:
            scored.append((sim, row))
    scored.sort(key=lambda t: -t[0])
    return [_format(r["task"], r["response"]) for _, r in scored[:k]]
