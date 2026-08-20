from sonder_runtime.application.memory.retrieval_evaluation import (
    RetrievedMemory,
    RetrievalLabel,
    evaluate_labeled_mapping,
    evaluate_retrieval,
)


def test_labeled_retrieval_reports_relevance_and_bounded_cost_metrics():
    metrics = evaluate_retrieval([
        RetrievalLabel(
            "q1", frozenset({"a", "b"}),
            retrieved=(RetrievedMemory("a", context_tokens=12), RetrievedMemory("x", context_tokens=8)),
            latency_ms=10,
        ),
        RetrievalLabel(
            "q2", frozenset({"c"}),
            retrieved=(RetrievedMemory("c", context_tokens=20),),
            latency_ms=20,
        ),
    ])
    assert metrics.relevant_precision == 2 / 3
    assert metrics.relevant_recall == 2 / 3
    assert metrics.contradiction_rate == 0.0
    assert metrics.latency_p95_ms == 20
    assert metrics.context_cost_tokens == 40 / 3
    assert 0 <= metrics.relevant_precision <= 1


def test_contradictory_and_stale_labels_are_measured_separately():
    metrics = evaluate_retrieval([
        RetrievalLabel(
            "contradiction",
            frozenset({"current"}),
            contradictory_ids=frozenset({"old"}),
            retrieved=(RetrievedMemory("old"), RetrievedMemory("current")),
        ),
        RetrievalLabel(
            "stale",
            frozenset({"new"}),
            stale_ids=frozenset({"expired"}),
            retrieved=(RetrievedMemory("expired", is_stale=True),),
        ),
    ])
    assert metrics.contradiction_rate == 1 / 3
    assert metrics.stale_recall == 1.0
    assert metrics.relevant_precision == 1 / 3


def test_k_caps_results_and_mapping_wrapper_is_deterministic():
    case = RetrievalLabel(
        "q",
        frozenset({"first"}),
        retrieved=(RetrievedMemory("first"), RetrievedMemory("noise")),
    )
    metrics = evaluate_labeled_mapping({"q": case}, k=1)
    assert metrics.evaluated_results == 1
    assert metrics.relevant_precision == 1.0


def test_empty_and_invalid_inputs_are_safe_and_bounded():
    empty = evaluate_retrieval([]).as_dict()
    assert empty["cases"] == 0
    assert empty["relevant_recall"] == 0.0
    try:
        evaluate_retrieval([RetrievalLabel("q", frozenset(), latency_ms=-1)])
    except ValueError as exc:
        assert "latency" in str(exc)
    else:
        raise AssertionError("negative latency must be rejected")
