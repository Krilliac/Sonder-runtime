import embeddings as e
import mmr_rerank as m


def test_empty_candidates_returns_empty():
    assert m.mmr_rerank([1.0, 0.0], [], k=3) == []


def test_zero_or_negative_k_returns_empty():
    candidates = [("A", [1.0, 0.0]), ("B", [0.0, 1.0])]
    assert m.mmr_rerank([1.0, 0.0], candidates, k=0) == []
    assert m.mmr_rerank([1.0, 0.0], candidates, k=-1) == []


def test_no_query_vec_falls_back_to_input_order():
    candidates = [("A", [1.0, 0.0]), ("B", [0.0, 1.0]), ("C", [0.5, 0.5])]
    assert m.mmr_rerank(None, candidates, k=2) == ["A", "B"]
    assert m.mmr_rerank([], candidates, k=2) == ["A", "B"]


def test_respects_k_smaller_than_pool():
    candidates = [("A", [1.0, 0.0]), ("B", [0.9, 0.1]), ("C", [0.0, 1.0])]
    out = m.mmr_rerank([1.0, 0.0], candidates, k=1)
    assert out == ["A"]


def test_lambda_1_is_pure_relevance_ranking():
    # lambda=1 strips out the diversity term entirely -> plain cosine-rank order.
    query = [1.0, 0.0]
    candidates = [
        ("low", [0.0, 1.0]),     # relevance 0.0
        ("high", [1.0, 0.0]),    # relevance 1.0
        ("mid", [0.6, 0.8]),     # relevance 0.6
    ]
    out = m.mmr_rerank(query, candidates, k=3, lambda_mult=1.0)
    assert out == ["high", "mid", "low"]


def test_mmr_suppresses_near_duplicate_in_favor_of_diverse_item():
    # A and B are near-duplicates with the highest raw relevance (0.8).
    # C has lower raw relevance (0.6) but is much less redundant with A.
    # Plain relevance ranking (lambda=1) would return [A, B]; MMR at
    # lambda=0.5 should prefer the diverse C for the second slot.
    query = [1.0, 0.0, 0.0]
    candidates = [
        ("A", [0.8, 0.6, 0.0]),
        ("B", [0.8, 0.6, 0.0]),   # exact duplicate of A
        ("C", [0.6, 0.0, 0.8]),
    ]

    pure_relevance = m.mmr_rerank(query, candidates, k=2, lambda_mult=1.0)
    assert pure_relevance == ["A", "B"]

    diversified = m.mmr_rerank(query, candidates, k=2, lambda_mult=0.5)
    assert diversified == ["A", "C"]


def test_lambda_0_ignores_query_after_first_pick():
    # First pick is still relevance-only (nothing selected yet to be
    # redundant with), but subsequent picks score purely on min-similarity
    # to what's already selected, ignoring the query entirely.
    query = [1.0, 0.0, 0.0]
    candidates = [
        ("A", [1.0, 0.0, 0.0]),   # highest relevance -> picked first
        ("B", [1.0, 0.0, 0.0]),   # duplicate of A -> maximally redundant
        ("C", [0.0, 1.0, 0.0]),   # orthogonal to A -> zero redundancy
    ]
    out = m.mmr_rerank(query, candidates, k=2, lambda_mult=0.0)
    assert out == ["A", "C"]


def test_duplicate_ids_are_treated_as_independent_candidates():
    # Same id twice with different vectors: both are eligible; MMR should
    # not collapse them via id-based dedup (that's the caller's job).
    candidates = [("X", [1.0, 0.0]), ("X", [0.0, 1.0])]
    out = m.mmr_rerank([1.0, 0.0], candidates, k=2, lambda_mult=0.5)
    assert out == ["X", "X"]


def test_custom_sim_fn_is_used_instead_of_cosine():
    calls = []

    def fake_sim(a, b):
        calls.append((a, b))
        return 1.0 if a == b else 0.0

    candidates = [("A", "v1"), ("B", "v2")]
    out = m.mmr_rerank("q", candidates, k=2, sim_fn=fake_sim)
    assert out == ["A", "B"]
    assert calls  # fake_sim was actually invoked, not embeddings.cosine


def test_mmr_from_blobs_decodes_and_skips_empty_blobs():
    query = [1.0, 0.0, 0.0]
    id_blob_pairs = [
        ("A", e.to_blob([0.8, 0.6, 0.0])),
        ("B", e.to_blob([0.8, 0.6, 0.0])),
        ("C", e.to_blob([0.6, 0.0, 0.8])),
        ("no-embedding", None),
        ("empty-embedding", b""),
    ]
    out = m.mmr_from_blobs(query, id_blob_pairs, k=2, lambda_mult=0.5)
    assert out == ["A", "C"]


def test_k_larger_than_pool_returns_all_without_error():
    candidates = [("A", [1.0, 0.0]), ("B", [0.0, 1.0])]
    out = m.mmr_rerank([1.0, 0.0], candidates, k=10)
    assert set(out) == {"A", "B"}
    assert len(out) == 2
