"""Semantic recall implementation over the migrated memory adapter."""
from __future__ import annotations

import os
from dataclasses import dataclass

import embeddings
import sonder_runtime.adapters.memory_store as memory_store
from sonder_runtime.application.ports.recall import validate_recall_request
from sonder_runtime.domain.common.errors import InvalidInput
from sonder_runtime.domain.memory import rules as _rules


DEFAULT_MIN_SIM = _rules.DEFAULT_RECALL_MIN_SIM
MAX_RESP_CHARS = 400


@dataclass(frozen=True)
class RecallPage:
    results: tuple[str, ...]
    incomplete: bool
    next_cursor: str | None
    termination: str
    candidates_examined: int
    candidates_scored: int


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


def recall_page(conn, task, k=2, embed_fn=None, min_sim=None,
                qv=None, exclude_session=None, project=None,
                include_all_projects=False, embedding_model=None,
                embedding_revision=None, candidate_cursor=None):
    """Return bounded recall results with truthful enumeration evidence."""
    include_all_projects = include_all_projects is True
    if min_sim is None:
        try:
            min_sim = float(
                os.environ.get("SONDER_RECALL_MIN_SIM", str(DEFAULT_MIN_SIM))
            )
        except (TypeError, ValueError) as exc:
            raise InvalidInput("recall similarity threshold is invalid") from exc
    validate_recall_request(task, k, min_sim)
    runtime_default = embed_fn is None
    embed_fn = embed_fn or embeddings.embed
    if qv is None:
        qv = embed_fn(task)
    if qv is None or not embeddings.valid_vector(qv):
        return RecallPage((), False, None, "no_query_embedding", 0, 0)
    query_provenance = embeddings.trusted_provenance(qv, embed_fn, runtime_default)
    if embedding_model is None:
        embedding_model = query_provenance.get("model")
    if embedding_revision is None:
        embedding_revision = query_provenance.get("revision")

    candidates = memory_store.good_interaction_candidate_page(
        conn,
        exclude_session,
        project=project,
        include_all_projects=include_all_projects,
        embedding_model=embedding_model,
        embedding_revision=embedding_revision,
        embedding_dim=len(qv),
        cursor=candidate_cursor,
    )
    scored = []
    scored_count = 0
    for candidate_rank, row in enumerate(candidates.rows):
        emb = row.get("task_embedding")
        if not emb or not isinstance(row.get("task"), str):
            continue
        response = row.get("response")
        if response is not None and not isinstance(response, str):
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
        scored_count += 1
        if _rules.passes_similarity(sim, min_sim):
            scored.append((sim, candidate_rank, row))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return RecallPage(
        results=tuple(
            _format(row["task"], row["response"])
            for _, _, row in scored[:k]
        ),
        incomplete=candidates.incomplete,
        next_cursor=candidates.next_cursor,
        termination=candidates.termination,
        candidates_examined=candidates.rows_examined,
        candidates_scored=scored_count,
    )


def recall(conn, task, k=2, embed_fn=None, min_sim=None,
           qv=None, exclude_session=None, project=None,
           include_all_projects=False, embedding_model=None,
           embedding_revision=None):
    """Compatibility list API over the bounded semantic-recall page."""
    page = recall_page(
        conn, task, k=k, embed_fn=embed_fn, min_sim=min_sim, qv=qv,
        exclude_session=exclude_session, project=project,
        include_all_projects=include_all_projects,
        embedding_model=embedding_model,
        embedding_revision=embedding_revision,
    )
    return list(page.results)
