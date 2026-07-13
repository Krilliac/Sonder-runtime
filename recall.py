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


def recall(conn, task, k=2, embed_fn=None, min_sim=None,
           qv=None, exclude_session=None, project=None,
           include_all_projects=False, embedding_model=None,
           embedding_revision=None):
    """Top-k good-outcome past interactions similar to `task`, formatted for injection.

    qv: precomputed query embedding (avoids a second embed call); if None it is
    computed from `task`. Recall is project-local by default; ``project=None``
    selects only unscoped rows. Cross-project recall requires the explicit
    ``include_all_projects`` override. Returns [] if embeddings are down or
    nothing clears min_sim.
    """
    include_all_projects = include_all_projects is True
    if min_sim is None:
        min_sim = float(os.environ.get("SONDER_RECALL_MIN_SIM", str(DEFAULT_MIN_SIM)))
    runtime_default = embed_fn is None
    embed_fn = embed_fn or embeddings.embed
    if qv is None:
        qv = embed_fn(task)
    if qv is None or not embeddings.valid_vector(qv):
        return []
    query_provenance = embeddings.provenance(qv)
    if embedding_model is None and (runtime_default or embed_fn is embeddings.embed):
        embedding_model = query_provenance.get("model")
    if embedding_revision is None and (runtime_default or embed_fn is embeddings.embed):
        embedding_revision = query_provenance.get("revision")

    scored = []
    for row in memory_store.good_interactions_with_embeddings(
        conn,
        exclude_session,
        project=project,
        include_all_projects=include_all_projects,
    ):
        emb = row.get("task_embedding")
        if not emb:
            continue
        try:
            stored = embeddings.from_blob(emb)
        except (TypeError, ValueError, EOFError):
            continue
        if not embeddings.valid_vector(stored) or len(stored) != len(qv):
            continue
        stored_dimension = row.get("task_embedding_dim")
        if (
            isinstance(stored_dimension, bool)
            or not isinstance(stored_dimension, int)
            or stored_dimension <= 0
            or stored_dimension != len(stored)
            or stored_dimension != len(qv)
        ):
            continue
        stored_model = row.get("task_embedding_model")
        stored_revision = row.get("task_embedding_revision")
        if embedding_model and stored_model != embedding_model:
            continue
        if (
            embedding_revision is not None
            and (stored_revision or None) != (embedding_revision or None)
        ):
            continue
        sim = embeddings.cosine(qv, stored)
        if sim >= min_sim:
            scored.append((sim, row))
    scored.sort(key=lambda t: -t[0])
    return [_format(r["task"], r["response"]) for _, r in scored[:k]]
