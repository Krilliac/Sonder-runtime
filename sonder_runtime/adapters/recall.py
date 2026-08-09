"""Semantic recall implementation over the migrated memory adapter."""
from __future__ import annotations

import os

import embeddings
import sonder_runtime.adapters.memory_store as memory_store
from sonder_runtime.domain.memory import rules as _rules


DEFAULT_MIN_SIM = _rules.DEFAULT_RECALL_MIN_SIM
MAX_RESP_CHARS = 400


def _fence_count(text):
    return sum(1 for line in text.splitlines() if line.lstrip().startswith("```"))


def _format(task, response, max_len=MAX_RESP_CHARS):
    resp = response or ""
    truncated = len(resp) > max_len
    if truncated:
        resp = resp[:max_len].rstrip() + " \u2026"
    line = "%s -> %s" % (task, resp)
    if truncated and _fence_count(line) % 2:
        line += "\n```"
    return line


def recall(conn, task, k=2, embed_fn=None, min_sim=None,
           qv=None, exclude_session=None, project=None,
           include_all_projects=False, embedding_model=None,
           embedding_revision=None):
    """Return project-scoped good outcomes similar to ``task``."""
    include_all_projects = include_all_projects is True
    if min_sim is None:
        min_sim = float(os.environ.get("SONDER_RECALL_MIN_SIM", str(DEFAULT_MIN_SIM)))
    runtime_default = embed_fn is None
    embed_fn = embed_fn or embeddings.embed
    if qv is None:
        qv = embed_fn(task)
    if qv is None or not embeddings.valid_vector(qv):
        return []
    query_provenance = embeddings.trusted_provenance(qv, embed_fn, runtime_default)
    if embedding_model is None:
        embedding_model = query_provenance.get("model")
    if embedding_revision is None:
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
        if _rules.passes_similarity(sim, min_sim):
            scored.append((sim, row))
    scored.sort(key=lambda item: -item[0])
    return [_format(row["task"], row["response"]) for _, row in scored[:k]]
