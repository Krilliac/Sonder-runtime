import memory_store as ms
import orchestrator as o


def test_build_prompt_no_lessons_is_passthrough():
    assert o.build_prompt("do X", []) == "do X"


def test_build_prompt_prepends_lessons():
    p = o.build_prompt("do X", ["lessonA", "lessonB"])
    assert "lessonA" in p and "lessonB" in p and "do X" in p
    assert p.index("lessonA") < p.index("do X")  # memories come first


def test_run_with_learning_captures_and_returns_id():
    c = ms.connect(":memory:")
    seen = {}

    def gen(prompt):
        seen["prompt"] = prompt
        return "the answer"

    resp, iid = o.run_with_learning(
        c, "fix the bug", "code", gen,
        retrieve_fn=lambda conn, task: ["prefer RRF"],
        id_fn=lambda: "fixed123",
    )
    assert resp == "the answer"
    assert iid == "fixed123"
    assert "prefer RRF" in seen["prompt"]          # retrieval was injected
    row = ms.get_interaction(c, "fixed123")
    assert row["task"] == "fix the bug"
    assert row["response"] == "the answer"
    assert row["tier"] == "code"
