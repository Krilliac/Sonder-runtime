"""/login keeps the bearer token for the session and never prints it (#16).

``/login alice <pw>`` printed ``token: 0d7a24de...`` after ``login ok``, to
the terminal and to ``repl --json`` stdout. The console now parses the token
into its session exactly as before and prints a masked line instead. The
``admin_login`` MCP tool and ``POST /v1/sonder/login`` still return the token:
that is their API contract.
"""
from __future__ import annotations

import sonder_runtime.interfaces.http.serve as ts
import sonder_runtime.interfaces.repl.repl as sonder_repl
from sonder_runtime.domain.login_output import HIDDEN_TOKEN_LINE, split_login_output

TOKEN = "0d7a24de" + "9f31c2b4a6e8d0f1"
LOGIN_OK = "login ok\nalice role=developer\ntoken: %s" % TOKEN


def test_split_keeps_the_token_and_masks_the_display():
    token, display = split_login_output(LOGIN_OK)
    assert token == TOKEN
    assert TOKEN not in display
    assert display.startswith("login ok\nalice role=developer")
    assert HIDDEN_TOKEN_LINE in display


def test_errors_and_tokenless_output_pass_through():
    assert split_login_output("ERROR: bad password") == ("", "ERROR: bad password")
    assert split_login_output("logged in") == ("", "logged in")
    assert split_login_output("ERROR: token: nope") == ("", "ERROR: token: nope")


def test_repl_login_sets_the_session_token_but_never_prints_it(monkeypatch, capsys):
    lines = iter(("/login alice pw-value-123", "/exit"))
    monkeypatch.setattr(sonder_repl, "_read_input", lambda *_a, **_k: next(lines))
    monkeypatch.setattr(sonder_repl, "_startup_banner", lambda *_args: "")
    monkeypatch.setattr(sonder_repl, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(sonder_repl, "_named_command_gate", lambda _cmd, _argument="": (True, ""))
    monkeypatch.setattr(sonder_repl, "_latest_repl_turn_metrics", lambda *_a, **_k: None)
    monkeypatch.setattr(sonder_repl.server, "admin_login", lambda *_args: LOGIN_OK)
    monkeypatch.setattr(sonder_repl, "CURRENT_TOKEN", "")

    sonder_repl.main()

    out = capsys.readouterr().out
    assert "login ok" in out
    assert TOKEN not in out
    assert sonder_repl.CURRENT_TOKEN == TOKEN


def test_served_console_login_masks_the_reply(monkeypatch):
    monkeypatch.setattr(ts, "_http_slash_refusal", lambda *_a, **_k: "")
    monkeypatch.setattr(ts.server, "admin_login", lambda username, password: LOGIN_OK)
    monkeypatch.setattr(ts.server, "_admin_account_from_token", lambda token: {"username": "alice"})
    state = ts.ConversationState()

    out = ts._handle_slash("/login alice pw-value-123", state=state)

    assert TOKEN not in out
    assert "login ok" in out
    assert state.token == TOKEN
