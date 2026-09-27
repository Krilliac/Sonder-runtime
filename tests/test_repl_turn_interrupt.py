"""Ctrl-C during a REPL turn cancels that turn, not the whole session.

The turn runs in its own foreground scope of the shared cancellation tree.
SIGINT cancels that scope (so later model requests and agent steps for the
turn are refused even if some layer swallowed the interrupt), unwinds the
blocking call, and returns to the prompt.  An interrupt at the idle prompt
still ends the session.
"""
import os
import signal
import time

import pytest

import server
import sonder_runtime.adapters.observability.activity_tracker as activity_tracker
import sonder_runtime.interfaces.repl.repl as sonder_repl
from sonder_runtime.application import foreground_turns
from sonder_runtime.application.cancellation_tree import CancellationTree


@pytest.fixture(autouse=True)
def _inject_legacy_runtime(monkeypatch):
    monkeypatch.setattr(sonder_repl, "_legacy_runtime", None)
    sonder_repl.configure_legacy_runtime(server)


def _prepare(monkeypatch, feed):
    monkeypatch.setattr(sonder_repl, "_read_input", lambda *_a, **_k: next(feed))
    monkeypatch.setattr(sonder_repl, "_startup_banner", lambda *_args: "")
    monkeypatch.setattr(sonder_repl, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(sonder_repl, "_named_command_gate", lambda _cmd, _argument="": (True, ""))
    monkeypatch.setattr(sonder_repl, "_begin_chat_turn", lambda *_a, **_k: None)
    monkeypatch.setattr(sonder_repl, "_latest_repl_turn_metrics", lambda *_a, **_k: None)
    monkeypatch.setattr(sonder_repl.command_router, "resolve", lambda _line: None)
    monkeypatch.setattr(sonder_repl.intents, "classify", lambda _line: None)
    monkeypatch.setattr(sonder_repl.intents, "containment_egress_refusal", lambda _line: None)
    monkeypatch.setattr(sonder_repl.intents, "classify_work", lambda _line: None)


def test_sigint_during_a_chat_turn_cancels_the_turn_and_returns_to_the_prompt(
        monkeypatch, capsys):
    calls = []
    seen = {}

    def fake_sonder(line, **_kwargs):
        calls.append(line)
        if line == "long question":
            try:
                os.kill(os.getpid(), signal.SIGINT)
                time.sleep(5)
            except KeyboardInterrupt:
                seen["scope_cancelled"] = foreground_turns.cancel_requested()
                seen["reason"] = foreground_turns.current().reason
                raise
            pytest.fail("SIGINT did not interrupt the turn")
        return "second answer"

    _prepare(monkeypatch, iter(("long question", "next question", "/exit")))
    monkeypatch.setattr(sonder_repl.server, "sonder", fake_sonder)
    previous_handler = signal.getsignal(signal.SIGINT)

    sonder_repl.main()

    assert calls == ["long question", "next question"]
    assert seen == {
        "scope_cancelled": True,
        "reason": foreground_turns.INTERRUPT_REASON,
    }
    out = capsys.readouterr().out
    assert "interrupted: turn cancelled" in out
    assert "second answer" in out
    # The turn-scoped handler is removed once the turn ends.
    assert signal.getsignal(signal.SIGINT) is previous_handler


def test_ctrl_c_at_the_idle_prompt_still_exits(monkeypatch):
    def interrupted(*_a, **_k):
        raise KeyboardInterrupt

    _prepare(monkeypatch, iter(()))
    monkeypatch.setattr(sonder_repl, "_read_input", interrupted)
    monkeypatch.setattr(
        sonder_repl.server, "sonder",
        lambda *_a, **_k: pytest.fail("no turn was submitted"),
    )

    assert sonder_repl.main() is None


def test_interrupt_clears_the_previous_turn_handles(monkeypatch, capsys):
    ran = []

    def fake_sonder(line, **_kwargs):
        if line == "first":
            return "```python\nprint('first answer')\n```"
        raise KeyboardInterrupt

    _prepare(monkeypatch, iter(("first", "second", "/run", "/exit")))
    monkeypatch.setattr(sonder_repl.server, "sonder", fake_sonder)
    monkeypatch.setattr(
        sonder_repl.code_runner, "run_code",
        lambda *a, **k: ran.append(a) or {"ok": True},
    )

    sonder_repl.main()

    assert ran == []
    out = capsys.readouterr().out
    assert "no code block in the last response" in out
    assert "interrupted: turn cancelled" in out


def test_cancelled_foreground_turn_refuses_further_model_requests(monkeypatch):
    posts = []
    monkeypatch.setattr(server, "_post", lambda *a, **k: posts.append(a) or {})

    with foreground_turns.foreground_turn("test") as node:
        foreground_turns.cancel(node)
        with pytest.raises(server.ModelCallError) as caught:
            server._post_model("/api/chat", {"model": "m"}, model="m")

    assert caught.value.kind == "cancelled"
    assert posts == []
    # Outside the cancelled scope, requests are unaffected.
    assert foreground_turns.cancel_requested() is False


def test_turn_cancellation_propagates_to_nested_scopes_and_is_discarded():
    with foreground_turns.foreground_turn("outer") as outer:
        with foreground_turns.foreground_turn("inner") as inner:
            foreground_turns.cancel(outer)
            assert inner.cancelled
            assert foreground_turns.cancel_requested()
    assert foreground_turns.current() is None
    assert list(foreground_turns._TREE.root.children()) == []


def test_tree_discard_rejects_root_and_forgets_subtree():
    tree = CancellationTree()
    parent = tree.create_child(node_id="a")
    tree.create_child("a", node_id="b")
    tree.discard("a")
    with pytest.raises(KeyError):
        tree.node("b")
    assert list(tree.root.children()) == []
    assert parent.status.value == "active"
    with pytest.raises(ValueError):
        tree.discard("root")


def test_activity_span_records_an_interrupted_turn_as_cancelled():
    with pytest.raises(KeyboardInterrupt):
        with activity_tracker.response_span("interrupt test", "prompt"):
            raise KeyboardInterrupt

    latest = activity_tracker.snapshot()["latest"]
    assert latest["status"] == "cancelled"
