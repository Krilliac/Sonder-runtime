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
    """
    if k <= 0 or not candidates:
        return []

    if not query_vec:
        # No query signal to diversify against: fall back to input order.
        return [cid for cid, _ in candidates[:k]]

    lambda_mult = max(0.0, min(1.0, lambda_mult))

    remaining = list(range(len(candidates)))
    selected = []

    while remaining and len(selected) < k:
        best_idx = None
        best_score = None
        for i in remaining:
            _, vec = candidates[i]
            relevance = sim_fn(query_vec, vec)
            if selected:
                redundancy = max(sim_fn(vec, candidates[j][1]) for j in selected)
            else:
                redundancy = 0.0
            score = lambda_mult * relevance - (1.0 - lambda_mult) * redundancy
            if best_score is None or score > best_score:
                best_score = score
                best_idx = i
        selected.append(best_idx)
        remaining.remove(best_idx)

    return [candidates[i][0] for i in selected]


def mmr_from_blobs(query_vec, id_blob_pairs, k=5, lambda_mult=0.5,
                    sim_fn=embeddings.cosine, from_blob=embeddings.from_blob):
    """Convenience wrapper for retriever.py-style storage: decode stored
    embedding blobs (as returned by memory_store's `embedding` column) into
    vectors, then MMR-rerank. Rows with no/empty blob are skipped, matching
    the None-embedding handling in retriever._semantic_rank.
    """
    candidates = [(cid, from_blob(blob)) for cid, blob in id_blob_pairs if blob]
    return mmr_rerank(query_vec, candidates, k=k, lambda_mult=lambda_mult, sim_fn=sim_fn)
