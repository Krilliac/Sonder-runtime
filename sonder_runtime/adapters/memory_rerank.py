"""Embedding-backed adapters for pure memory ranking policy."""
from __future__ import annotations

import sonder_runtime.adapters.embeddings as embeddings
from sonder_runtime.domain.memory import rules


def mmr_rerank(
    query_vec,
    candidates,
    k=5,
    lambda_mult=0.5,
    sim_fn=embeddings.cosine,
):
    """Select relevant, diverse candidates with the configured similarity."""
    return rules.mmr_select(
        query_vec,
        candidates,
        k=k,
        lambda_mult=lambda_mult,
        sim_fn=sim_fn,
    )


def mmr_from_blobs(
    query_vec,
    id_blob_pairs,
    k=5,
    lambda_mult=0.5,
    sim_fn=embeddings.cosine,
    from_blob=embeddings.from_blob,
):
    """Decode stored embeddings, omit empty values, and apply MMR."""
    candidates = [
        (candidate_id, from_blob(blob))
        for candidate_id, blob in id_blob_pairs
        if blob
    ]
    return mmr_rerank(
        query_vec,
        candidates,
        k=k,
        lambda_mult=lambda_mult,
        sim_fn=sim_fn,
    )
