"""Maximal Marginal Relevance (MMR) reranker for retrieval candidates.

Given a query vector and a pool of (id, vector) candidates, greedily builds a
top-k list that trades off query relevance against redundancy with items
already picked. This suppresses near-duplicate lessons that plain
cosine-similarity or RRF ranking would otherwise stack at the top (e.g. five
near-identical restatements of the same lesson crowding out a genuinely
different one).

Pure stdlib, no I/O. The only external dependency is the similarity function,
which is injected (defaults to embeddings.cosine, itself pure).

    score(candidate) = lambda_mult * relevance(candidate, query)
                        - (1 - lambda_mult) * max(similarity(candidate, already_selected))

lambda_mult=1.0  -> plain relevance ranking (no diversity pressure).
lambda_mult=0.0  -> pure diversity (ignores the query after the first pick).
"""
import embeddings

from sonder_runtime.domain.memory import rules as _rules


def mmr_rerank(query_vec, candidates, k=5, lambda_mult=0.5, sim_fn=embeddings.cosine):
    """Greedy MMR selection.

    query_vec: query embedding, or falsy to skip diversification entirely.
    candidates: list of (id, vector) pairs. ids need not be unique; vectors
        need not be unit-normalized (sim_fn handles that).
    k: max number of ids to return.
    lambda_mult: relevance/diversity tradeoff, clamped to [0, 1].
    sim_fn: similarity(vec_a, vec_b) -> float, higher = more similar.

    Returns a list of candidate ids, length min(k, len(candidates)), in
    selection order (most relevant/diverse first). Order among exact score
    ties favors the earlier candidate in the input list (stable, deterministic).

    SPEC-3 Phase 4: the pure selection lives in
    ``sonder_runtime.domain.memory.rules.mmr_select``; this wrapper supplies
    the embedding similarity default so callers are unchanged.
    """
    return _rules.mmr_select(
        query_vec, candidates, k=k, lambda_mult=lambda_mult, sim_fn=sim_fn,
    )


def mmr_from_blobs(query_vec, id_blob_pairs, k=5, lambda_mult=0.5,
                    sim_fn=embeddings.cosine, from_blob=embeddings.from_blob):
    """Convenience wrapper for retriever.py-style storage: decode stored
    embedding blobs (as returned by memory_store's `embedding` column) into
    vectors, then MMR-rerank. Rows with no/empty blob are skipped, matching
    the None-embedding handling in retriever._semantic_rank.
    """
    candidates = [(cid, from_blob(blob)) for cid, blob in id_blob_pairs if blob]
    return mmr_rerank(query_vec, candidates, k=k, lambda_mult=lambda_mult, sim_fn=sim_fn)
