import embeddings
import memory_store as ms
import recall


def _conn():
    return ms.connect(":memory:")


def _store_good(c, iid, task, response, vec, session_id=None):
    ms.log_interaction(c, iid, task, "", response, "sonder",
                       session_id=session_id, task_embedding=embeddings.to_blob(vec))
    ms.record_outcome_row(c, iid, "tests_passed", 1.0)


def test_recall_returns_similar_good_solution():
    c = _conn()
    _store_good(c, "i1", "reverse a string", "def rev(s): return s[::-1]", [1.0, 0.0])
    _store_good(c, "i2", "parse json", "import json", [0.0, 1.0])
    # query embedding aligned with i1
    out = recall.recall(c, "reverse text", k=2, embed_fn=lambda t: [1.0, 0.0], min_sim=0.9)
    assert len(out) == 1
    assert "reverse a string" in out[0]
    assert "s[::-1]" in out[0]


def test_recall_respects_min_sim_threshold():
    c = _conn()
    _store_good(c, "i1", "task", "resp", [1.0, 0.0])
    # orthogonal query -> cosine 0 -> below threshold
    assert recall.recall(c, "q", embed_fn=lambda t: [0.0, 1.0], min_sim=0.5) == []


def test_recall_soft_fails_when_no_embeddings():
    c = _conn()
    _store_good(c, "i1", "task", "resp", [1.0, 0.0])
    assert recall.recall(c, "q", embed_fn=lambda t: None) == []


def test_recall_excludes_current_session():
    c = _conn()
    _store_good(c, "i1", "task", "resp", [1.0, 0.0], session_id="cur")
    out = recall.recall(c, "q", embed_fn=lambda t: [1.0, 0.0], min_sim=0.5,
                        exclude_session="cur")
    assert out == []


def test_recall_ignores_bad_outcomes():
    c = _conn()
    ms.log_interaction(c, "i1", "task", "", "resp", "sonder",
                       task_embedding=embeddings.to_blob([1.0, 0.0]))
    ms.record_outcome_row(c, "i1", "failed", -1.0)
    assert recall.recall(c, "q", embed_fn=lambda t: [1.0, 0.0], min_sim=0.5) == []


def test_recall_truncates_long_responses():
    c = _conn()
    long_resp = "x" * 1000
    _store_good(c, "i1", "task", long_resp, [1.0, 0.0])
    out = recall.recall(c, "q", embed_fn=lambda t: [1.0, 0.0], min_sim=0.5)
    assert len(out[0]) < 1000
    assert out[0].endswith("…")
