import threading

import memory_store
import pytest
import server


def _prepared_candidate(lesson_id="LNEW", text="Use pathlib.Path for path joins."):
    return {
        "status": "candidate",
        "lesson_id": lesson_id,
        "text": text,
        "embedding": None,
        "embedding_blob": None,
        "embedding_model": None,
        "embedding_revision": None,
        "embedding_dim": None,
    }


def test_record_outcome_credits_retrieved_lessons(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "mem.db"))
    conn = server._open_db()
    try:
        memory_store.add_lesson(conn, "L1", "use deque for queues", None, "seed")
        memory_store.log_interaction(conn, "I1", "task", "use deque", "answer", "code")
        memory_store.log_lesson_usage(conn, ["L1"], "I1", "task")
    finally:
        conn.close()
    monkeypatch.setattr(
        server.reflection,
        "prepare_lesson_candidate",
        lambda *args, **kwargs: {"status": "no_lesson", "reason": "duplicate"},
    )
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

    monkeypatch.setattr(
        server.reflection,
        "prepare_lesson_candidate",
        lambda *args, **kwargs: _prepared_candidate(),
    )
    out = server.learn_from_example("join paths", "from pathlib import Path", "accepted")
    assert "Learned lesson LNEW" in out
    conn = server._open_db()
    try:
        assert memory_store.get_lesson_text(conn, "LNEW") == "Use pathlib.Path for path joins."
    finally:
        conn.close()


def test_record_outcome_same_signal_is_idempotent_and_distills_once(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "mem.db"))
    conn = server._open_db()
    try:
        memory_store.log_interaction(conn, "I1", "task", "", "answer", "code")
    finally:
        conn.close()
    calls = []
    monkeypatch.setattr(
        server.reflection,
        "prepare_lesson_candidate",
        lambda *args, **kwargs: calls.append(args) or _prepared_candidate(),
    )

    first = server.record_outcome("I1", "tests_passed")
    second = server.record_outcome("I1", "tests_passed")

    assert first.startswith("Recorded 'tests_passed'")
    assert "Distilled lesson LNEW" in first
    assert second.startswith("Already recorded 'tests_passed'")
    assert len(calls) == 1
    conn = server._open_db()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM outcomes WHERE interaction_id='I1'"
        ).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM lessons").fetchone()[0] == 1
    finally:
        conn.close()


def test_record_outcome_retryable_duplicate_retries_without_duplicate_evidence(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "mem.db"))
    conn = server._open_db()
    try:
        memory_store.log_interaction(conn, "I1", "task", "", "answer", "code")
    finally:
        conn.close()
    calls = []

    def prepare(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise RuntimeError("temporary model failure")
        return _prepared_candidate()

    monkeypatch.setattr(server.reflection, "prepare_lesson_candidate", prepare)

    first = server.record_outcome("I1", "tests_passed")
    second = server.record_outcome("I1", "tests_passed")

    assert "deferred for retry" in first
    assert second.startswith("Already recorded 'tests_passed'")
    assert "Distilled lesson LNEW" in second
    assert len(calls) == 2
    conn = server._open_db()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM outcomes WHERE interaction_id='I1'"
        ).fetchone()[0] == 1
        row = conn.execute(
            "SELECT state, attempts FROM lesson_distillations "
            "WHERE interaction_id='I1'"
        ).fetchone()
        assert (row["state"], row["attempts"]) == (
            memory_store.DISTILLATION_STORED,
            2,
        )
    finally:
        conn.close()


def test_interruption_after_atomic_claim_releases_exact_token(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "mem.db"))
    conn = server._open_db()
    try:
        memory_store.log_interaction(conn, "I1", "task", "", "answer", "code")
    finally:
        conn.close()
    original = memory_store.record_outcome_and_claim_lesson_distillation

    def interrupt_after_commit(*args, **kwargs):
        original(*args, **kwargs)
        raise KeyboardInterrupt()

    monkeypatch.setattr(
        server.memory_store,
        "record_outcome_and_claim_lesson_distillation",
        interrupt_after_commit,
    )
    with pytest.raises(KeyboardInterrupt):
        server.record_outcome("I1", "tests_passed")

    conn = server._open_db()
    try:
        row = conn.execute(
            "SELECT state, claim_token FROM lesson_distillations "
            "WHERE interaction_id='I1'"
        ).fetchone()
        assert row["state"] == memory_store.DISTILLATION_RETRYABLE
        assert row["claim_token"] is None
    finally:
        conn.close()


def test_release_io_failure_uses_same_process_abandon_marker(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "mem.db"))
    conn = server._open_db()
    try:
        memory_store.log_interaction(conn, "I1", "task", "", "answer", "code")
    finally:
        conn.close()
    prepare_calls = []

    def prepare(*args, **kwargs):
        prepare_calls.append(args)
        if len(prepare_calls) == 1:
            raise RuntimeError("temporary model failure")
        return _prepared_candidate()

    monkeypatch.setattr(server.reflection, "prepare_lesson_candidate", prepare)
    original_mark = memory_store.mark_lesson_distillation_retryable
    monkeypatch.setattr(
        server.memory_store,
        "mark_lesson_distillation_retryable",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("db unavailable")),
    )
    first = server.record_outcome("I1", "tests_passed")
    monkeypatch.setattr(
        server.memory_store,
        "mark_lesson_distillation_retryable",
        original_mark,
    )
    second = server.record_outcome("I1", "tests_passed")

    assert "deferred for retry" in first
    assert "Distilled lesson LNEW" in second
    assert len(prepare_calls) == 2


def test_contradictory_outcome_cancels_inflight_distillation(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "mem.db"))
    conn = server._open_db()
    try:
        memory_store.log_interaction(conn, "I1", "task", "", "answer", "code")
    finally:
        conn.close()
    started = threading.Event()
    release = threading.Event()
    result = {}

    def prepare(*args, **kwargs):
        started.set()
        assert release.wait(timeout=5)
        return _prepared_candidate()

    monkeypatch.setattr(server.reflection, "prepare_lesson_candidate", prepare)

    worker = threading.Thread(
        target=lambda: result.setdefault(
            "good", server.record_outcome("I1", "tests_passed"),
        ),
    )
    worker.start()
    assert started.wait(timeout=5)
    failed = server.record_outcome("I1", "failed")
    release.set()
    worker.join(timeout=10)

    assert not worker.is_alive()
    assert failed.startswith("Recorded 'failed'")
    assert "Distilled lesson" not in result["good"]
    conn = server._open_db()
    try:
        assert conn.execute("SELECT COUNT(*) FROM lessons").fetchone()[0] == 0
        assert conn.execute(
            "SELECT state FROM lesson_distillations WHERE interaction_id='I1'"
        ).fetchone()[0] == memory_store.DISTILLATION_CANCELLED
    finally:
        conn.close()


def test_code_gate_failure_uses_idempotent_atomic_outcome_path(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "mem.db"))
    conn = server._open_db()
    try:
        memory_store.log_interaction(conn, "I1", "task", "", "answer", "code")
        memory_store.add_lesson(conn, "L1", "seed lesson", None, "seed")
        memory_store.log_lesson_usage(conn, ["L1"], "I1", "task")
    finally:
        conn.close()

    server._record_code_gate_failure("I1")
    server._record_code_gate_failure("I1")

    conn = server._open_db()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM outcomes WHERE interaction_id='I1' "
            "AND signal='failed'"
        ).fetchone()[0] == 1
        stats = memory_store.lesson_usage_stats(conn)["L1"]
        assert stats["losses"] == 1
    finally:
        conn.close()
