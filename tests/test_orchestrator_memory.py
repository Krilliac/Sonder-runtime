import memory_store as ms
import orchestrator as o
import retriever


def test_build_prompt_orders_facts_lessons_recalls_then_task():
    p = o.build_prompt("do X", ["lessonA"], recalls=["recallA"], facts=["factA"])
    assert p.index("factA") < p.index("lessonA") < p.index("recallA") < p.index("do X")
    assert o.FACTS_HEADER in p and o.RECALL_HEADER in p and o.MEMORY_HEADER in p


def test_build_prompt_empty_all_is_passthrough():
    assert o.build_prompt("do X", [], recalls=[], facts=[]) == "do X"


def test_build_prompt_only_recalls():
    p = o.build_prompt("do X", [], recalls=["saw this before"])
    assert "saw this before" in p and "do X" in p
    assert o.MEMORY_HEADER not in p  # no lessons block when lessons empty


def test_history_is_passed_to_generate_fn():
    c = ms.connect(":memory:")
    seen = {}

    def gen(prompt, history=None):
        seen["prompt"] = prompt
        seen["history"] = history
        return "ans"

    hist = [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "ok"}]
    resp, iid = o.run_with_learning(
        c, "now", "sonder", gen,
        retrieve_fn=lambda conn, task: [], id_fn=lambda: "id1",
        history=hist, session_id="S",
    )
    assert resp == "ans"
    assert seen["history"] == hist
    row = ms.get_interaction(c, "id1")
    assert row["session_id"] == "S"


def test_no_history_calls_single_arg_gen():
    c = ms.connect(":memory:")

    def gen(prompt):  # 1-arg gen (legacy style) must still work when no history
        return "ans"

    resp, iid = o.run_with_learning(
        c, "now", "sonder", gen,
        retrieve_fn=lambda conn, task: [], id_fn=lambda: "id2",
    )
    assert resp == "ans"


def test_recalls_and_facts_reach_the_prompt():
    c = ms.connect(":memory:")
    seen = {}

    def gen(prompt):
        seen["prompt"] = prompt
        return "ans"

    o.run_with_learning(
        c, "task", "sonder", gen,
        retrieve_fn=lambda conn, t: ["a lesson"], id_fn=lambda: "id3",
        recalls=["a recall"], facts=["a fact"],
    )
    assert "a fact" in seen["prompt"]
    assert "a lesson" in seen["prompt"]
    assert "a recall" in seen["prompt"]


def test_prompt_and_trace_omit_quarantined_lesson(monkeypatch):
    conn = ms.connect(":memory:")
    bad_text = "threading lock advice that repeatedly failed"
    good_text = "threading lock advice with neutral evidence"
    ms.add_lesson(conn, "bad", bad_text, None, "seed")
    ms.add_lesson(conn, "good", good_text, None, "seed")
    for index in range(retriever.QUARANTINE_REPEAT_TASK_MIN_LOSSES):
        interaction_id = "bad-use-%s" % index
        ms.log_lesson_usage(conn, ["bad"], interaction_id, "threading lock")
        ms.record_lesson_usage_outcome(conn, interaction_id, "failed", -1.0)

    real_retrieve = retriever.retrieve_with_ids
    monkeypatch.setattr(
        retriever,
        "retrieve_with_ids",
        lambda stored, task: real_retrieve(
            stored, task, embed_fn=lambda _text: None,
        ),
    )

    _response, _interaction_id, trace = o.run_with_learning_traced(
        conn, "threading lock advice", "code", lambda prompt: "answer",
        id_fn=lambda: "new-interaction",
    )

    assert good_text in trace["lessons"]
    assert bad_text not in trace["lessons"]
    assert good_text in trace["augmented_prompt"]
    assert bad_text not in trace["augmented_prompt"]
