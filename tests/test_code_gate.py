"""Chat-path code gate: compile+smoke-run runnable Python replies.

Regression suite for two probes that shipped runtime-broken code with zero
verification (dateutil ParserError / timedelta TypeError). The gate reuses the
grounding sandbox already trusted by parallel_generate and /run.
"""
import threading

import pytest

import memory_store
import server


def setup_function():
    server.activity_tracker.reset_for_tests()


GOOD_REPLY = (
    "Here is a parser:\n\n```python\n"
    "def double(x):\n    return x * 2\n\n"
    "assert double(2) == 4\nprint('ok')\n"
    "```\n"
)
BAD_REPLY = (
    "Here is a parser:\n\n```python\n"
    "def parse(s):\n    return undefined_helper(s)\n\n"
    "print(parse('P3D'))\n"
    "```\n"
)
FIXED_REPLY = (
    "Corrected:\n\n```python\n"
    "def parse(s):\n    return s.strip('P')\n\n"
    "print(parse('P3D'))\n"
    "```\n"
)


# --- gate target selection -----------------------------------------------------


def test_gate_target_requires_fenced_python_with_definitions():
    assert server._code_gate_target(GOOD_REPLY) is not None
    assert server._code_gate_target("no code here at all") is None
    # Trivial snippet without def/class/import: not worth the latency.
    assert server._code_gate_target("```python\nprint(1 + 1)\n```") is None
    # Non-Python fences are out of scope for now.
    assert server._code_gate_target("```js\nconst f = () => 1;\n```") is None


def test_gate_target_skips_interactive_samples():
    reply = "```python\ndef ask():\n    return input('name? ')\nask()\n```"
    assert server._code_gate_target(reply) is None


# --- gate outcomes ---------------------------------------------------------------


def test_good_code_verifies_and_reply_is_unchanged():
    reply, verified, repaired = server._apply_code_gate(GOOD_REPLY)

    assert verified is True
    assert repaired is False
    assert reply == GOOD_REPLY


def test_failing_code_gets_banner_and_negative_outcome(monkeypatch):
    recorded = []
    monkeypatch.setattr(
        server, "_record_code_gate_failure", lambda iid: recorded.append(iid),
    )

    reply, verified, repaired = server._apply_code_gate(
        BAD_REPLY, interaction_id="iid-9",
    )

    assert verified is False
    assert repaired is False
    assert "NOT VERIFIED" in reply
    assert "NameError" in reply
    assert reply.startswith(BAD_REPLY.rstrip("\n").split("\n")[0])
    assert recorded == ["iid-9"]


def test_repair_round_trip_returns_fixed_reply(monkeypatch):
    recorded = []
    monkeypatch.setattr(
        server, "_record_code_gate_failure", lambda iid: recorded.append(iid),
    )
    prompts = []

    def regenerate(repair_prompt):
        prompts.append(repair_prompt)
        return FIXED_REPLY

    reply, verified, repaired = server._apply_code_gate(
        BAD_REPLY, interaction_id="iid-10", regenerate=regenerate,
    )

    assert verified is True
    assert repaired is True
    assert reply == FIXED_REPLY
    assert "fails when run" in prompts[0]
    assert "NameError" in prompts[0]
    assert recorded == []


def test_failed_repair_keeps_original_reply_with_banner(monkeypatch):
    recorded = []
    monkeypatch.setattr(
        server, "_record_code_gate_failure", lambda iid: recorded.append(iid),
    )

    reply, verified, repaired = server._apply_code_gate(
        BAD_REPLY, interaction_id="iid-11", regenerate=lambda p: BAD_REPLY,
    )

    assert verified is False
    assert repaired is False
    assert "NOT VERIFIED" in reply
    assert reply.startswith("Here is a parser:")
    assert recorded == ["iid-11"]


def test_timeout_is_inconclusive_not_failure(monkeypatch):
    monkeypatch.setattr(
        server.grounding, "run_code_detail",
        lambda *a, **k: {
            "ok": False, "timed_out": True, "stdout": "", "stderr": "",
            "returncode": None, "error": "timed out after 8s", "timeout": 8,
        },
    )

    reply, verified, repaired = server._apply_code_gate(
        GOOD_REPLY, interaction_id="iid-12",
    )

    assert verified is None
    assert repaired is False
    assert reply == GOOD_REPLY


def test_env_kill_switch_disables_gate(monkeypatch):
    monkeypatch.setenv("SONDER_CODE_GATE", "0")

    reply, verified, repaired = server._apply_code_gate(
        BAD_REPLY, interaction_id="iid-13",
    )

    assert verified is None
    assert repaired is False
    assert reply == BAD_REPLY


def test_repair_timeout_returns_original_without_repaired_identity(monkeypatch):
    results = iter([
        {
            "ok": False, "timed_out": False, "stdout": "",
            "stderr": "NameError", "returncode": 1,
        },
        {
            "ok": False, "timed_out": True, "stdout": "",
            "stderr": "", "returncode": None,
        },
    ])
    monkeypatch.setattr(
        server.grounding, "run_code_detail", lambda *a, **k: next(results),
    )
    recorded = []
    monkeypatch.setattr(
        server, "_record_code_gate_failure", lambda iid: recorded.append(iid),
    )

    reply, verified, repaired = server._apply_code_gate(
        BAD_REPLY, interaction_id="iid-timeout", regenerate=lambda p: FIXED_REPLY,
    )

    assert reply.startswith(BAD_REPLY)
    assert "NOT VERIFIED" in reply
    assert "repair verification timed out" in reply
    assert verified is False
    assert repaired is False
    assert recorded == ["iid-timeout"]


@pytest.mark.parametrize(
    ("original_source", "repair_source", "expected_source"),
    [
        ("ollama", "ollama", "ollama+code-repair"),
        ("estimated", "estimated", "estimated+code-repair"),
        ("ollama", "estimated", "mixed+code-repair"),
    ],
)
def test_repair_persistence_aggregates_usage_and_provenance(
    monkeypatch, original_source, repair_source, expected_source,
):
    captured = {}

    class Connection:
        def close(self):
            captured["closed"] = True

    monkeypatch.setattr(server, "_open_db", lambda: Connection())

    def replace(conn, interaction_id, **kwargs):
        captured.update(kwargs)
        captured["interaction_id"] = interaction_id
        return True

    monkeypatch.setattr(
        server.memory_store, "replace_interaction_response_cas", replace,
    )
    expected = {
        "tokens_in": 10,
        "tokens_out": 20,
        "token_source": original_source,
    }

    assert server._persist_verified_code_repair(
        "iid-usage", expected, FIXED_REPLY,
        {"tokens_in": 3, "tokens_out": 4, "token_source": repair_source},
    ) is True
    assert captured["interaction_id"] == "iid-usage"
    assert captured["response"] == FIXED_REPLY
    assert captured["tokens_in"] == 13
    assert captured["tokens_out"] == 24
    assert captured["token_source"] == expected_source
    assert captured["closed"] is True


# --- chat-surface wiring ---------------------------------------------------------


def test_sonder_impl_banners_broken_code(monkeypatch):
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server.web_tools, "enabled", lambda: False)
    monkeypatch.setattr(
        server, "_serve_target",
        lambda tier, strict: ("fake-model", False, True, "sonder"),
    )
    monkeypatch.setattr(
        server, "_answer", lambda *a, **k: (BAD_REPLY, "iid-14", None),
    )
    # Repair generation fails fast (no model in tests).
    monkeypatch.setattr(
        server, "_make_generate",
        lambda *a, **k: (lambda p, h=None: (_ for _ in ()).throw(RuntimeError())),
    )
    recorded = []
    monkeypatch.setattr(
        server, "_record_code_gate_failure", lambda iid: recorded.append(iid),
    )

    out = server._sonder_impl(
        "write a duration parser", session="none", project="none",
    )

    assert "NOT VERIFIED" in out
    assert "[interaction_id: iid-14]" in out
    assert recorded == ["iid-14"]


def test_sonder_impl_repairs_broken_code(monkeypatch):
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server.web_tools, "enabled", lambda: False)
    monkeypatch.setattr(
        server, "_serve_target",
        lambda tier, strict: ("fake-model", False, True, "sonder"),
    )
    monkeypatch.setattr(
        server, "_answer", lambda *a, **k: (BAD_REPLY, "iid-15", None),
    )
    monkeypatch.setattr(
        server, "_make_generate",
        lambda *a, **k: (lambda p, h=None: FIXED_REPLY),
    )
    monkeypatch.setattr(server, "_persist_verified_code_repair", lambda *a: True)

    out = server._sonder_impl(
        "write a duration parser", session="none", project="none",
    )

    assert "Corrected:" in out
    assert "NOT VERIFIED" not in out
    assert "[interaction_id: iid-15]" in out


def test_sonder_impl_suppresses_stale_footer_when_repair_cas_loses(monkeypatch):
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server.web_tools, "enabled", lambda: False)
    monkeypatch.setattr(
        server, "_serve_target",
        lambda tier, strict: ("fake-model", False, True, "sonder"),
    )
    monkeypatch.setattr(
        server, "_answer", lambda *a, **k: (BAD_REPLY, "iid-conflict", None),
    )
    monkeypatch.setattr(
        server, "_make_generate", lambda *a, **k: (lambda p, h=None: FIXED_REPLY),
    )
    monkeypatch.setattr(server, "_persist_verified_code_repair", lambda *a: False)

    out = server._sonder_impl(
        "write a duration parser", session="none", project="none",
    )

    assert "Corrected:" in out
    assert "[interaction_id:" not in out


def test_remembered_session_turns_serialize_through_repair_boundary(monkeypatch):
    first_entered = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    second_entered = threading.Event()
    completed = []

    def fake_impl(prompt, **kwargs):
        if prompt == "first":
            first_entered.set()
            assert release_first.wait(2)
        else:
            second_entered.set()
        completed.append(prompt)
        return prompt

    monkeypatch.setattr(server, "_sonder_impl_serialized", fake_impl)

    first = threading.Thread(
        target=lambda: server._sonder_impl("first", session="shared"),
    )

    def run_second():
        second_started.set()
        server._sonder_impl("second", session="shared")

    second = threading.Thread(target=run_second)
    first.start()
    assert first_entered.wait(1)
    second.start()
    assert second_started.wait(1)
    assert not second_entered.wait(0.1)
    release_first.set()
    first.join(2)
    second.join(2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert second_entered.is_set()
    assert completed == ["first", "second"]
    assert server._SESSION_TURN_LOCKS == {}


def test_distinct_sessions_remain_concurrent(monkeypatch):
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()

    def fake_impl(prompt, **kwargs):
        if prompt == "first":
            first_entered.set()
            assert release_first.wait(2)
        else:
            second_entered.set()
        return prompt

    monkeypatch.setattr(server, "_sonder_impl_serialized", fake_impl)
    first = threading.Thread(
        target=lambda: server._sonder_impl("first", session="session-a"),
    )
    second = threading.Thread(
        target=lambda: server._sonder_impl("second", session="session-b"),
    )

    first.start()
    assert first_entered.wait(1)
    second.start()
    assert second_entered.wait(1)
    release_first.set()
    first.join(2)
    second.join(2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert server._SESSION_TURN_LOCKS == {}


def test_persistent_session_claim_blocks_other_runtime_namespace(
    monkeypatch, tmp_path,
):
    db_path = tmp_path / "persistent-session-claim.db"
    monkeypatch.setattr(
        server, "_open_db", lambda: memory_store.connect(str(db_path)),
    )
    monkeypatch.setattr(server, "_SESSION_TURN_CLAIM_WAIT_SECONDS", 0)
    owner = memory_store.connect(str(db_path))
    assert memory_store.claim_session_turn(
        owner, "shared", "other-process",
    )
    entered = []
    monkeypatch.setattr(
        server, "_sonder_impl_serialized",
        lambda *args, **kwargs: entered.append(1) or "unexpected",
    )

    try:
        reply = server._sonder_impl("second", session="shared")
    finally:
        memory_store.release_session_turn(owner, "shared", "other-process")
        owner.close()

    assert "already has a turn in progress" in reply
    assert entered == []


def test_release_failure_marks_completed_claim_safely_reclaimable(monkeypatch):
    abandoned = []

    class BrokenConnection:
        def close(self):
            return None

    monkeypatch.setattr(
        server.memory_store,
        "release_session_turn",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("db unavailable")),
    )
    monkeypatch.setattr(
        server, "_open_db",
        lambda: (_ for _ in ()).throw(OSError("db unavailable")),
    )
    monkeypatch.setattr(
        server.memory_store,
        "abandon_session_turn_claim",
        lambda *owner: abandoned.append(owner) or True,
    )

    server._release_persistent_session_turn({
        "conn": BrokenConnection(),
        "session_id": "shared",
        "claim_token": "completed-token",
        "owner_pid": 123,
        "owner_identity": "windows:456",
    })

    assert abandoned == [(
        "shared", "completed-token", 123, "windows:456",
    )]


def test_sonder_impl_leaves_verified_code_alone(monkeypatch):
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server.web_tools, "enabled", lambda: False)
    monkeypatch.setattr(
        server, "_serve_target",
        lambda tier, strict: ("fake-model", False, True, "sonder"),
    )
    monkeypatch.setattr(
        server, "_answer", lambda *a, **k: (GOOD_REPLY, "iid-16", None),
    )

    out = server._sonder_impl(
        "write a doubler", session="none", project="none",
    )

    assert "NOT VERIFIED" not in out
    assert "def double" in out
    assert "[interaction_id: iid-16]" in out


def test_answer_with_history_gates_code_too(monkeypatch):
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server.web_tools, "enabled", lambda: False)
    monkeypatch.setattr(
        server, "_serve_target",
        lambda tier, strict: ("fake-model", False, True, "sonder"),
    )
    monkeypatch.setattr(
        server, "_answer", lambda *a, **k: (BAD_REPLY, "iid-17", None),
    )
    monkeypatch.setattr(
        server, "_make_generate",
        lambda *a, **k: (lambda p, h=None: (_ for _ in ()).throw(RuntimeError())),
    )
    recorded = []
    monkeypatch.setattr(
        server, "_record_code_gate_failure", lambda iid: recorded.append(iid),
    )

    out = server._answer_with_history_impl("write a duration parser", [])

    assert "NOT VERIFIED" in out
    assert recorded == ["iid-17"]


def _install_persistent_repair_fakes(monkeypatch, db_path, interaction_id):
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server.web_tools, "enabled", lambda: False)
    monkeypatch.setattr(
        server, "_serve_target",
        lambda tier, strict: ("fake-model", False, True, "sonder"),
    )
    monkeypatch.setattr(
        server, "_open_db", lambda: memory_store.connect(str(db_path)),
    )

    def fake_answer(conn, prompt, *args, **kwargs):
        memory_store.log_interaction(
            conn, interaction_id, prompt, "lesson context", BAD_REPLY, "sonder",
            tokens_in=10, tokens_out=20, token_source="ollama",
            project=kwargs.get("project"), project_explicit=True,
        )
        return BAD_REPLY, interaction_id, None

    monkeypatch.setattr(server, "_answer", fake_answer)

    def make_generate(*args, **kwargs):
        def generate(prompt, history=None):
            return FIXED_REPLY

        generate.last_usage = {
            "tokens_in": 3, "tokens_out": 4, "token_source": "ollama",
        }
        return generate

    monkeypatch.setattr(server, "_make_generate", make_generate)


def _assert_persisted_repair(db_path, interaction_id):
    conn = memory_store.connect(str(db_path))
    try:
        row = memory_store.get_interaction(conn, interaction_id)
    finally:
        conn.close()
    assert row["response"] == FIXED_REPLY
    assert row["tokens_in"] == 13
    assert row["tokens_out"] == 24
    assert row["token_source"] == "ollama+code-repair"


def test_sonder_impl_persists_verified_repair(monkeypatch, tmp_path):
    db_path = tmp_path / "sonder-repair.db"
    _install_persistent_repair_fakes(monkeypatch, db_path, "iid-persist-sonder")

    out = server._sonder_impl(
        "write a duration parser", session="none", project="none",
    )

    assert "[interaction_id: iid-persist-sonder]" in out
    _assert_persisted_repair(db_path, "iid-persist-sonder")


def test_history_impl_persists_verified_repair(monkeypatch, tmp_path):
    db_path = tmp_path / "history-repair.db"
    _install_persistent_repair_fakes(monkeypatch, db_path, "iid-persist-history")
    monkeypatch.setattr(server, "_should_learn", lambda tier, learn: True)

    out = server._answer_with_history_impl("write a duration parser", [])

    assert "[interaction_id: iid-persist-history]" in out
    _assert_persisted_repair(db_path, "iid-persist-history")
