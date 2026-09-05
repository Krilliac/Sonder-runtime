import threading
import memory_store
import pytest
import server
from sonder_runtime.domain.common.errors import InvalidInput


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
        memory_store.record_lesson_usage_outcome(conn, "I1", "tests_passed", 1.0, source="caller")
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


@pytest.mark.parametrize("tool_name", ["record_outcome", "learn_from_example"])
def test_learning_tools_reject_unknown_signals_with_typed_error(tool_name):
    # Other boundary tests reload server after collection. Resolve the current
    # callable here instead of comparing an obsolete function object by identity.
    tool = getattr(server, tool_name)
    args = ("I1",) if tool_name == "record_outcome" else ("task", "solution")
    with pytest.raises(InvalidInput, match="unknown signal 'bogus'"):
        tool(*args, signal="bogus")


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


def test_record_outcome_defers_without_model_call_while_fleet_is_active(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "mem.db"))
    conn = server._open_db()
    try:
        memory_store.log_interaction(conn, "I1", "task", "", "answer", "code")
    finally:
        conn.close()

    monkeypatch.setattr(
        server.master_orchestrator, "active_model_call_count", lambda: 2,
    )
    prepare_calls = []
    monkeypatch.setattr(
        server.reflection,
        "prepare_lesson_candidate",
        lambda *args, **kwargs: prepare_calls.append((args, kwargs)),
    )

    out = server.record_outcome("I1", "edited")

    assert out.startswith("Recorded 'edited'")
    assert "deferred for retry" in out
    assert prepare_calls == []
    conn = server._open_db()
    try:
        row = conn.execute(
            "SELECT state, claim_token, last_error FROM lesson_distillations "
            "WHERE interaction_id='I1'"
        ).fetchone()
        assert row["state"] == memory_store.DISTILLATION_RETRYABLE
        assert row["claim_token"] is None
        assert row["last_error"] == "distillation deferred: active fleet model calls"
        assert conn.execute(
            "SELECT COUNT(*) FROM outcomes WHERE interaction_id='I1' "
            "AND signal='edited'"
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_record_outcome_uses_short_shared_distillation_budget(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "mem.db"))
    conn = server._open_db()
    try:
        memory_store.log_interaction(conn, "I1", "task", "", "answer", "code")
    finally:
        conn.close()

    monkeypatch.setattr(
        server.master_orchestrator, "active_model_call_count", lambda: 0,
    )
    monkeypatch.setattr(
        server, "_distillation_timeout_policy", lambda *_args: 7,
    )
    calls = []

    def generate(prompt, **kwargs):
        calls.append(("generate", prompt, kwargs["timeout"]))
        return "candidate text"

    def embed(text, timeout=30):
        calls.append(("embed", text, timeout))
        return None

    def prepare(task, response, signal, offload_fn, embed_fn):
        offload_fn("distill prompt")
        embed_fn("candidate text")
        return {"status": "no_lesson", "reason": "test"}

    monkeypatch.setattr(server, "_gateway_generate_text", generate)
    monkeypatch.setattr(server.embeddings, "embed", embed)
    monkeypatch.setattr(server.reflection, "prepare_lesson_candidate", prepare)

    out = server.record_outcome("I1", "tests_passed")

    assert out.startswith("Recorded 'tests_passed'")
    assert [row[0] for row in calls] == ["generate", "embed"]
    assert all(1 <= row[2] <= 7 for row in calls)


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


def test_drain_deferred_distillations_retries_fleet_deferred_jobs(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "mem.db"))
    conn = server._open_db()
    try:
        memory_store.log_interaction(conn, "I1", "task", "", "answer", "code")
        memory_store.log_interaction(conn, "I2", "task", "", "answer", "code")
    finally:
        conn.close()
    candidates = iter(("LNEW1", "LNEW2", "LNEW3", "LNEW4"))

    def _next_candidate(*args, **kwargs):
        lesson_id = next(candidates)
        return _prepared_candidate(
            lesson_id=lesson_id,
            text="Use pathlib.Path for path joins (%s)." % lesson_id,
        )

    monkeypatch.setattr(
        server.reflection, "prepare_lesson_candidate", _next_candidate,
    )

    # A busy fleet defers distillation for both campaign outcomes.
    monkeypatch.setattr(
        server.master_orchestrator, "active_model_call_count", lambda: 2,
    )
    assert "deferred for retry" in server.record_outcome("I1", "tests_passed")
    assert "deferred for retry" in server.record_outcome("I2", "tests_passed")

    # A busy fleet also blocks the drain itself. Pinned as an exact dict on
    # purpose -- that is what stops a bucket being dropped silently -- but the
    # shape is now the FULL one: a blocked drain reports every bucket a real
    # drain does. It used to return a short dict, so a caller reading
    # `drain["failed"]` or `drain["backlog"]` off the blocked path would have
    # raised, or worse, read a `.get` default as a measured zero.
    assert server._drain_deferred_distillations() == {
        "drained": 0, "stored": 0, "deferred": 0,
        "failed": 0, "skipped": 0, "backlog": None,
    }

    # Once quiet, the drain stores the deferred lessons without new outcomes.
    monkeypatch.setattr(
        server.master_orchestrator, "active_model_call_count", lambda: 0,
    )
    drain = server._drain_deferred_distillations()
    assert drain["drained"] == 2
    assert drain["stored"] == 2
    assert drain["deferred"] == 0
    # Nothing raised and nothing fell between the buckets, so the counts are
    # totals rather than floors.
    assert drain["failed"] == 0
    assert drain["skipped"] == 0

    conn = server._open_db()
    try:
        for interaction_id in ("I1", "I2"):
            assert conn.execute(
                "SELECT COUNT(*) FROM outcomes WHERE interaction_id=?",
                (interaction_id,),
            ).fetchone()[0] == 1
            state = conn.execute(
                "SELECT state FROM lesson_distillations WHERE interaction_id=?",
                (interaction_id,),
            ).fetchone()[0]
            assert state == memory_store.DISTILLATION_STORED
        assert conn.execute(
            "SELECT COUNT(*) FROM lesson_distillations WHERE state='retryable'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_prepare_lesson_candidate_bounded_matches_reflection_interface(
    monkeypatch,
):
    """The bounded offload closure must accept reflection.distill's exact
    call shape — an interface drift here fails every real distillation with
    TypeError while tests that mock prepare_lesson_candidate stay green."""
    seen = {}

    def fake_generate_text(prompt, tier="fast", system="", temperature=0.2,
                           num_predict=256, num_ctx=2048, timeout=None):
        seen.update(tier=tier, system=system, num_predict=num_predict,
                    timeout=timeout)
        return "Use pathlib.Path for path joins."

    monkeypatch.setattr(server, "_gateway_generate_text", fake_generate_text)
    monkeypatch.setattr(server.embeddings, "embed", lambda *a, **k: None)

    interaction = {
        "id": "I-real",
        "task": "join two paths",
        "response": "used pathlib",
        "tier": "code",
    }
    candidate = server._prepare_lesson_candidate_bounded(
        interaction, "tests_passed",
    )
    assert isinstance(candidate, dict)
    assert seen["tier"] == "code"
    assert seen["system"]
    assert seen["timeout"] is not None


def test_failure_distills_a_pitfall_lesson(monkeypatch, tmp_path):
    """Failures used to teach nothing: 167 recorded failures had produced 0
    lessons, so an identical parse error recurred every night."""
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "mem.db"))
    conn = server._open_db()
    try:
        memory_store.log_interaction(conn, "F1", "task", "", "answer", "code")
    finally:
        conn.close()
    monkeypatch.setattr(
        server, "_gateway_generate_text",
        lambda prompt, **kw: "Use ${name}: to delimit a PowerShell variable "
                             "before a colon, which otherwise parses as a drive.",
    )
    monkeypatch.setattr(server.embeddings, "embed", lambda *a, **k: None)

    lesson_id, note = server._record_failure_pitfall(
        "F1", "print a count", "Write-Output \"$word:$count\"",
        "Variable reference is not valid. ':' was not followed by a valid "
        "variable name character.",
    )
    assert lesson_id
    assert note == "", "a clean distillation must not report a diagnostic"
    conn = server._open_db()
    try:
        row = conn.execute(
            "SELECT text, source_interaction FROM lessons WHERE id=?",
            (lesson_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert "${" in row["text"]
    assert row["source_interaction"] == "F1"


def test_pitfall_requires_a_concrete_error(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "mem.db"))
    conn = server._open_db()
    try:
        memory_store.log_interaction(conn, "F2", "task", "", "answer", "code")
    finally:
        conn.close()
    monkeypatch.setattr(server, "_gateway_generate_text", lambda prompt, **kw: "NONE")
    monkeypatch.setattr(server.embeddings, "embed", lambda *a, **k: None)
    # An empty error never reaches the model at all.
    assert server._record_failure_pitfall("F2", "task", "code", "") == ("", "")
    # A vague/NONE answer is refused by the existing vagueness gate. A refusal
    # is a completed run, so it reports no diagnostic - only a crash does.
    assert server._record_failure_pitfall(
        "F2", "task", "code", "pytest timed out") == ("", "")


def test_pitfall_crash_is_reported_not_swallowed(monkeypatch, tmp_path):
    """A refusal and a crash both returned "", so a pitfall path that broke in
    an unattended run was indistinguishable from one with nothing to learn.
    The overnight run of 2026-08-03 recorded 28 failures and stored 0 lessons,
    and telling those two apart took a manual replay. It must stay
    best-effort - the campaign attempt that fed it may not fail."""
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "mem.db"))

    def explode(*_args, **_kwargs):
        raise RuntimeError("distiller offline")

    monkeypatch.setattr(server.reflection, "prepare_pitfall_candidate", explode)

    lesson_id, note = server._record_failure_pitfall(
        "F3", "task", "code", "Variable reference is not valid.",
    )
    assert lesson_id == ""
    assert "RuntimeError" in note and "distiller offline" in note


def test_record_outcome_routes_through_unit_of_work(monkeypatch, tmp_path):
    """SPEC-3: the outcome write + distillation claim go through the
    UnitOfWork port, bound to the server's database path."""
    from sonder_runtime.adapters.unit_of_work import UnitOfWorkAdapter as LegacyUnitOfWork

    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "mem.db"))
    conn = server._open_db()
    try:
        memory_store.log_interaction(conn, "U1", "task", "", "answer", "code")
    finally:
        conn.close()

    seen = {}

    class _RecordingUow(LegacyUnitOfWork):
        def __init__(self, db_path=None):
            seen["db_path"] = db_path
            super().__init__(db_path)

    class _App:
        unit_of_work = _RecordingUow

    monkeypatch.setattr(server, "_application", lambda: _App())
    monkeypatch.setattr(
        server, "_prepare_lesson_candidate_bounded",
        lambda interaction, signal: {"status": "no_lesson", "reason": "test"},
    )
    out = server.record_outcome("U1", "tests_passed")
    assert out.startswith("Recorded 'tests_passed'")
    assert seen["db_path"] == server._DB_PATH


def test_learn_from_example_routes_through_unit_of_work(monkeypatch, tmp_path):
    """SPEC-3: the example interaction write goes through the UnitOfWork
    port, bound to the server's database path."""
    from sonder_runtime.adapters.unit_of_work import UnitOfWorkAdapter as LegacyUnitOfWork

    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "mem.db"))
    monkeypatch.setattr(server.embeddings, "embed", lambda *a, **k: None)

    seen = {}

    class _RecordingUow(LegacyUnitOfWork):
        def __init__(self, db_path=None):
            seen.setdefault("db_paths", []).append(db_path)
            super().__init__(db_path)

    class _App:
        unit_of_work = _RecordingUow

    monkeypatch.setattr(server, "_application", lambda: _App())
    monkeypatch.setattr(
        server, "_prepare_lesson_candidate_bounded",
        lambda interaction, signal: {"status": "no_lesson", "reason": "test"},
    )
    out = server.learn_from_example("join paths", "use pathlib", "accepted")
    assert not out.startswith("ERROR")
    # Both the interaction write and the outcome flow used the port,
    # each bound to the server's path.
    assert seen["db_paths"] and all(
        p == server._DB_PATH for p in seen["db_paths"]
    )


def test_record_outcome_persists_the_refusal_reason_reflection_computed(
    monkeypatch, tmp_path,
):
    """server.py must stop discarding the reason the distiller already knows.

    reflection.store_prepared_lesson computes the exact reason a candidate was
    refused, finalize_lesson_distillation returns it, and server.py dropped it
    on the floor. The measured cost: answering "where is distillation yield
    lost?" meant replaying 80 historical interactions through a live local
    model, because the ledger recorded only 'stored' / 'no_lesson'. This test
    drives the real record_outcome path -- real reflection, real finalizer --
    and requires the reason to reach both the ledger row and the caller.
    """
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "mem.db"))
    conn = server._open_db()
    try:
        memory_store.log_interaction(conn, "I1", "task", "", "answer", "code")
    finally:
        conn.close()
    # What the real distiller returns when the model produced nothing concrete.
    monkeypatch.setattr(
        server.reflection,
        "prepare_lesson_candidate",
        lambda *args, **kwargs: {"status": "no_lesson", "reason": "not_concrete"},
    )

    result = server._record_outcome_and_maybe_distill(
        "I1", "tests_passed", source="caller",
    )

    assert result["distillation_state"] == memory_store.DISTILLATION_NO_LESSON
    assert result["distillation_reason"] == "not_concrete"
    conn = server._open_db()
    try:
        assert conn.execute(
            "SELECT result_reason FROM lesson_distillations "
            "WHERE interaction_id='I1'"
        ).fetchone()["result_reason"] == "not_concrete"
    finally:
        conn.close()


def test_record_outcome_persists_a_real_dedupe_refusal_reason(monkeypatch, tmp_path):
    """A dedupe refusal must be distinguishable from a weak-candidate refusal.

    Both collapse to 'no_lesson' on the ledger, yet they mean opposite things:
    one says the distiller produced nothing, the other says the corpus already
    knows it. Conflating them is what made a dead semantic-dedupe gate invisible
    in production -- a reason breakdown would have shown zero semantic_duplicate
    rows across the entire corpus. Here the reason comes from the real dedupe
    code, not a stubbed return value.
    """
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "mem.db"))
    text = "Use pathlib.Path for path joins."
    conn = server._open_db()
    try:
        memory_store.add_lesson(conn, "LOLD", text, None, "seed")
        memory_store.log_interaction(conn, "I1", "task", "", "answer", "code")
    finally:
        conn.close()
    monkeypatch.setattr(
        server.reflection,
        "prepare_lesson_candidate",
        lambda *args, **kwargs: _prepared_candidate(text=text),
    )

    result = server._record_outcome_and_maybe_distill(
        "I1", "tests_passed", source="caller",
    )

    assert result["distillation_state"] == memory_store.DISTILLATION_NO_LESSON
    assert result["distillation_reason"] == "exact_duplicate"
    conn = server._open_db()
    try:
        assert memory_store.distillation_reason_counts(conn) == [
            {
                "state": memory_store.DISTILLATION_NO_LESSON,
                "reason": "exact_duplicate",
                "count": 1,
            },
        ]
    finally:
        conn.close()
