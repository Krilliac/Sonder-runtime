import memory_store as ms
import orchestrator as o


def test_build_prompt_no_lessons_is_passthrough():
    assert o.build_prompt("do X", []) == "do X"


def test_build_prompt_prepends_lessons():
    p = o.build_prompt("do X", ["lessonA", "lessonB"])
    assert "lessonA" in p and "lessonB" in p and "do X" in p
    assert p.index("lessonA") < p.index("do X")  # memories come first
    assert "How to apply the lessons" in p


def test_build_prompt_injects_run_compat_for_game_failure_request():
    p = o.build_prompt("create python games in increasing complexity tell failure", [])

    assert "/run compatibility requirements" in p
    assert "Do not use input()" in p
    assert "scripted smoke-test" in p
    assert "create python games in increasing complexity tell failure" in p


def test_build_prompt_injects_run_compat_for_explicit_run_request():
    p = o.build_prompt("make a pygame demo that will run with /run", [])

    assert "/run compatibility requirements" in p
    assert "Do not include `/run ...`" in p


def test_build_prompt_injects_language_specific_run_compat_for_cpp():
    p = o.build_prompt("make a simple console rpg game in C++ that will run with /run", [])

    assert "```cpp code block" in p
    assert "complete runnable C++ source" in p
    assert "keyboard input" in p
    assert "/runwindow" in p


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
        project="project-a",
    )
    assert resp == "the answer"
    assert iid == "fixed123"
    assert "prefer RRF" in seen["prompt"]          # retrieval was injected
    row = ms.get_interaction(c, "fixed123")
    assert row["task"] == "fix the bug"
    assert row["response"] == "the answer"
    assert row["tier"] == "code"
    assert row["project"] == "project-a"
    assert row["tokens_in"] > 0
    assert row["tokens_out"] > 0
    assert row["token_source"] == "estimated"


def test_run_with_learning_persists_generator_token_usage():
    c = ms.connect(":memory:")

    def gen(prompt):
        gen.last_usage = {
            "tokens_in": 123,
            "tokens_out": 45,
            "token_source": "ollama",
        }
        return "the answer"

    resp, iid = o.run_with_learning(
        c, "fix the bug", "code", gen,
        retrieve_fn=lambda conn, task: [],
        id_fn=lambda: "tok123",
    )
    assert resp == "the answer"
    row = ms.get_interaction(c, iid)
    assert row["tokens_in"] == 123
    assert row["tokens_out"] == 45
    assert row["token_source"] == "ollama"


def test_run_with_learning_still_returns_2_tuple():
    c = ms.connect(":memory:")
    result = o.run_with_learning(
        c, "fix the bug", "code", lambda prompt: "the answer",
        retrieve_fn=lambda conn, task: ["prefer RRF"],
        id_fn=lambda: "fixed123",
    )
    assert len(result) == 2
    resp, iid = result
    assert resp == "the answer"
    assert iid == "fixed123"


def test_run_with_learning_traced_returns_trace_context():
    c = ms.connect(":memory:")
    seen = {}

    def gen(prompt):
        seen["prompt"] = prompt
        return "the answer"

    resp, iid, trace = o.run_with_learning_traced(
        c, "fix the bug", "code", gen,
        retrieve_fn=lambda conn, task: ["prefer RRF"],
        id_fn=lambda: "fixed123",
    )
    assert resp == "the answer"
    assert iid == "fixed123"
    assert trace["lessons"] == ["prefer RRF"]
    assert "prefer RRF" in trace["augmented_prompt"]
    assert "fix the bug" in trace["augmented_prompt"]
    assert trace["augmented_prompt"] == seen["prompt"]


def test_default_retriever_logs_lesson_usage(monkeypatch):
    import retriever
    c = ms.connect(":memory:")
    ms.add_lesson(c, "L1", "use deque for queues", None, "seed")

    monkeypatch.setattr(
        retriever,
        "retrieve_with_ids",
        lambda conn, task: [{"id": "L1", "text": "use deque for queues", "score": 1.0}],
    )
    resp, iid = o.run_with_learning(
        c, "queue task", "code", lambda prompt: "answer", id_fn=lambda: "I1")
    assert resp == "answer"
    stats = ms.lesson_usage_stats(c)["L1"]
    assert stats["uses"] == 1


def test_default_retriever_still_logs_usage_after_hot_reload(monkeypatch):
    import importlib
    import retriever

    c = ms.connect(":memory:")
    ms.add_lesson(c, "L1", "use deque for queues", None, "seed")
    reloaded = importlib.reload(retriever)
    monkeypatch.setattr(
        reloaded,
        "retrieve_with_ids",
        lambda conn, task: [
            {"id": "L1", "text": "use deque for queues", "score": 1.0},
        ],
    )

    o.run_with_learning(
        c, "queue task", "code", lambda prompt: "answer", id_fn=lambda: "I2",
    )

    assert ms.lesson_usage_stats(c)["L1"]["uses"] == 1
