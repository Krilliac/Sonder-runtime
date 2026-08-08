"""Semantic recall of whole past solutions (not just distilled lessons).

Finds prior interactions whose *task* is semantically close to the current one AND
that ended in a good outcome, so sonder can reuse what already worked. Mirrors
retriever.py's cosine + min_sim approach, but over interactions instead of lessons.
Soft-fails to [] whenever embeddings are unavailable — never raises.
"""
import os

import embeddings
import memory_store
from sonder_runtime.domain.memory import rules as _rules

# Stricter than lessons' 0.62 (retriever.DEFAULT_MIN_SIM, recalibrated
# 2026-07-06): a recall injects a whole task+solution, so we only want
# genuinely close matches. This comment used to cite the lesson floor as 0.65 —
# the value that recalibration measured as WRONG — so anyone re-tuning this
# floor reasoned from a baseline 0.03 off. Env-overridable like SONDER_MIN_SIM.
# SPEC-3 Phase 4: the floor default and threshold rule live in the domain layer.
DEFAULT_MIN_SIM = _rules.DEFAULT_RECALL_MIN_SIM
MAX_RESP_CHARS = 400


def _fence_count(text):
    return sum(1 for line in text.splitlines() if line.lstrip().startswith("```"))


def _format(task, response, max_len=MAX_RESP_CHARS):
    resp = response or ""
    truncated = len(resp) > max_len
    if truncated:
        resp = resp[:max_len].rstrip() + " …"
    line = "%s -> %s" % (task, resp)
    # The cut often lands inside a code block: of the 1682 responses in the
    # live store long enough to truncate, 473 (28%) ended with an unbalanced
    # fence once assembled. A recall is the LAST block build_prompt emits
    # before "# Task:", so an unterminated ``` swallowed the header, the user's
    # real question and the model's own reply into a code block -- the
    # instruction stopped reading as an instruction.
    # Balance is judged on the assembled line, not on the response alone: 63%
    # of those responses open with a fence on their FIRST line, which lands
    # after the "task -> " prefix and so opens nothing -- closing it there
    # would open a block rather than close one.
    if truncated and _fence_count(line) % 2:
        line += "\n```"
    return line


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
    scored.sort(key=lambda t: -t[0])
    return [_format(r["task"], r["response"]) for _, r in scored[:k]]
