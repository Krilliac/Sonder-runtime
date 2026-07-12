"""Chat-path code gate: compile+smoke-run runnable Python replies.

Regression suite for two probes that shipped runtime-broken code with zero
verification (dateutil ParserError / timedelta TypeError). The gate reuses the
grounding sandbox already trusted by parallel_generate and /run.
"""
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
    reply, verified = server._apply_code_gate(GOOD_REPLY)

    assert verified is True
    assert reply == GOOD_REPLY


def test_failing_code_gets_banner_and_negative_outcome(monkeypatch):
    recorded = []
    monkeypatch.setattr(
        server, "_record_code_gate_failure", lambda iid: recorded.append(iid),
    )

    reply, verified = server._apply_code_gate(BAD_REPLY, interaction_id="iid-9")

    assert verified is False
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

    reply, verified = server._apply_code_gate(
        BAD_REPLY, interaction_id="iid-10", regenerate=regenerate,
    )

    assert verified is True
    assert reply == FIXED_REPLY
    assert "fails when run" in prompts[0]
    assert "NameError" in prompts[0]
    assert recorded == []


def test_failed_repair_keeps_original_reply_with_banner(monkeypatch):
    recorded = []
    monkeypatch.setattr(
        server, "_record_code_gate_failure", lambda iid: recorded.append(iid),
    )

    reply, verified = server._apply_code_gate(
        BAD_REPLY, interaction_id="iid-11", regenerate=lambda p: BAD_REPLY,
    )

    assert verified is False
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

    reply, verified = server._apply_code_gate(GOOD_REPLY, interaction_id="iid-12")

    assert verified is None
    assert reply == GOOD_REPLY


def test_env_kill_switch_disables_gate(monkeypatch):
    monkeypatch.setenv("SONDER_CODE_GATE", "0")

    reply, verified = server._apply_code_gate(BAD_REPLY, interaction_id="iid-13")

    assert verified is None
    assert reply == BAD_REPLY


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

    out = server._sonder_impl(
        "write a duration parser", session="none", project="none",
    )

    assert "Corrected:" in out
    assert "NOT VERIFIED" not in out
    assert "[interaction_id: iid-15]" in out


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
