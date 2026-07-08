import embeddings
import memory_store
import server


def test_record_outcome_credits_retrieved_lessons(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "mem.db"))
    conn = server._open_db()
    try:
        memory_store.add_lesson(conn, "L1", "use deque for queues", None, "seed")
        memory_store.log_interaction(conn, "I1", "task", "use deque", "answer", "code")
        memory_store.log_lesson_usage(conn, ["L1"], "I1", "task")
    finally:
        conn.close()
    monkeypatch.setattr(server.reflection, "maybe_add_lesson", lambda *a, **k: None)
    out = server.record_outcome("I1", "tests_passed")
    assert "Recorded" in out
    conn = server._open_db()
    try:
        stats = memory_store.lesson_usage_stats(conn)["L1"]
    finally:
        conn.close()
    assert stats["wins"] == 1
    assert stats["avg_reward"] > 0


def test_apply_learned_returns_usage_stats(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "mem.db"))
    monkeypatch.setattr(server.embeddings, "embed", lambda text: None)
    conn = server._open_db()
    try:
        memory_store.add_lesson(conn, "L1", "use deque for queue operations", None, "seed")
        memory_store.log_lesson_usage(conn, ["L1"], "I1", "queue task")
        memory_store.record_lesson_usage_outcome(conn, "I1", "tests_passed", 1.0)
    finally:
        conn.close()
    monkeypatch.setattr(
        server.retriever,
        "retrieve_with_ids",
        lambda conn, task, k=5: [{"id": "L1", "text": "use deque for queue operations"}],
    )
    out = server.apply_learned("queue operations")
    assert "use deque" in out
    assert "wins=1" in out


def test_learn_from_example_records_distilled_lesson(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "mem.db"))
    monkeypatch.setattr(server.embeddings, "embed", lambda text: None)

    def fake_maybe_add(conn, interaction_id, task, response, signal, **kwargs):
        memory_store.add_lesson(conn, "LNEW", "Use pathlib.Path for path joins.", None, interaction_id)
        return "LNEW"

    monkeypatch.setattr(server.reflection, "maybe_add_lesson", fake_maybe_add)
    out = server.learn_from_example("join paths", "from pathlib import Path", "accepted")
    assert "Learned lesson LNEW" in out
    conn = server._open_db()
    try:
        assert memory_store.get_lesson_text(conn, "LNEW") == "Use pathlib.Path for path joins."
    finally:
        conn.close()
