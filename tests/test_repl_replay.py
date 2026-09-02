"""Contracts for the /replay console command and the error-panel hints."""
import pytest

import server
import sonder_runtime.interfaces.repl.repl as sonder_repl


@pytest.fixture(autouse=True)
def _inject_legacy_runtime(monkeypatch):
    monkeypatch.setattr(sonder_repl, "_legacy_runtime", None)
    sonder_repl.configure_legacy_runtime(server)


class _Conn:
    def close(self):
        pass


def _drive(monkeypatch, lines):
    feed = iter(tuple(lines) + ("/exit",))
    monkeypatch.setattr(sonder_repl, "_read_input", lambda *_a, **_k: next(feed))
    monkeypatch.setattr(sonder_repl, "_startup_banner", lambda *_args: "")
    monkeypatch.setattr(sonder_repl, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(sonder_repl, "_named_command_gate", lambda _cmd, _argument="": (True, ""))
    monkeypatch.setattr(
        sonder_repl.server, "sonder",
        lambda *_a, **_k: pytest.fail("replay must not run a model turn"),
    )
    sonder_repl.main()


def test_replay_shows_the_current_thread_without_a_model_turn(
        monkeypatch, capsys):
    monkeypatch.setattr(sonder_repl.server, "_open_db", lambda: _Conn())
    monkeypatch.setattr(
        sonder_repl.memory_store, "session_turns",
        lambda _conn, _sid: [
            {"id": "i1", "task": "q1", "response": "a1"},
            {"id": "i2", "task": "q2", "response": "a2"},
        ],
    )
    monkeypatch.setattr(
        sonder_repl.memory_store, "find_session",
        lambda *_a: pytest.fail("bare /replay must use the current thread"),
    )

    _drive(monkeypatch, ("/replay",))

    out = capsys.readouterr().out
    assert "2 turn(s)" in out
    assert "[1] you    | q1" in out
    assert "    sonder | a2" in out


def test_replay_resolves_a_named_thread_and_bounds_the_window(
        monkeypatch, capsys):
    seen = {}

    def _find(_conn, prefix):
        seen["prefix"] = prefix
        return "otherid"

    monkeypatch.setattr(sonder_repl.server, "_open_db", lambda: _Conn())
    monkeypatch.setattr(sonder_repl.memory_store, "find_session", _find)
    monkeypatch.setattr(
        sonder_repl.memory_store, "session_turns",
        lambda _conn, sid: [
            {"id": "i%d" % n, "task": "q%d" % n, "response": "a%d" % n}
            for n in range(1, 4)
        ] if sid == "otherid" else pytest.fail("wrong session"),
    )

    _drive(monkeypatch, ("/replay fix the parser 1",))

    out = capsys.readouterr().out
    assert seen["prefix"] == "fix the parser"
    assert "replay otherid" in out
    assert "showing last 1" in out
    assert "[3] you    | q3" in out
    assert "q1" not in out


def test_replay_reports_an_unknown_thread_instead_of_guessing(
        monkeypatch, capsys):
    monkeypatch.setattr(sonder_repl.server, "_open_db", lambda: _Conn())
    monkeypatch.setattr(
        sonder_repl.memory_store, "find_session", lambda _conn, _p: None,
    )
    monkeypatch.setattr(
        sonder_repl.memory_store, "session_turns",
        lambda *_a: pytest.fail("an unresolved thread must not be read"),
    )

    _drive(monkeypatch, ("/replay missing",))

    out = capsys.readouterr().out
    assert "no session matching 'missing'" in out
    assert "/sessions" in out


def test_help_advertises_replay_next_to_sessions_and_resume():
    help_lines = sonder_repl.HELP.splitlines()
    replay = next(l for l in help_lines if l.strip().startswith("/replay"))
    assert "[id|title] [N]" in replay
    index = {line.split()[0]: n for n, line in enumerate(
        line.strip() for line in help_lines) if line.strip().startswith("/")}
    assert index["/sessions"] < index["/replay"] < index["/resume"]


def test_interactive_error_panel_adds_a_hint_for_known_failures(
        monkeypatch, capsys):
    monkeypatch.setattr(sonder_repl, "_console_has_operator", lambda: True)
    monkeypatch.setattr(sonder_repl, "_stdout_is_interactive", lambda: True)
    monkeypatch.setattr(sonder_repl._Ansi, "enabled", False)

    sonder_repl._print_chat_result(
        "ERROR contacting local Ollama at 127.0.0.1:11434 after 1 attempt(s): boom",
        0.0, error=True,
    )

    out = capsys.readouterr().out
    assert "hint: " in out
    assert "make sure it is running" in out


def test_interactive_error_panel_stays_hint_free_for_unknown_failures(
        monkeypatch, capsys):
    monkeypatch.setattr(sonder_repl, "_console_has_operator", lambda: True)
    monkeypatch.setattr(sonder_repl, "_stdout_is_interactive", lambda: True)
    monkeypatch.setattr(sonder_repl._Ansi, "enabled", False)

    sonder_repl._print_chat_result("ERROR: novel", 0.0, error=True)

    assert "hint: " not in capsys.readouterr().out


def test_piped_error_output_never_carries_a_hint(monkeypatch, capsys):
    monkeypatch.setattr(sonder_repl, "_console_has_operator", lambda: False)
    monkeypatch.setattr(
        sonder_repl, "_completion_timing",
        lambda _started: "Sonder completed in 1.00s",
    )
    monkeypatch.setattr(sonder_repl._Ansi, "enabled", False)
    monkeypatch.delenv("SONDER_REPL_NDJSON", raising=False)

    sonder_repl._print_chat_result(
        "ERROR contacting local Ollama at x after 1 attempt(s): boom",
        0.0, error=True,
    )

    out = capsys.readouterr().out
    assert "hint: " not in out
    assert out.endswith("[Sonder completed in 1.00s]\n")
