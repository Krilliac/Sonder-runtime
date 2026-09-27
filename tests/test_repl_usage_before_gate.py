"""Argument validation happens before the console permission gate.

The gate grades a command by the tools its branch can reach.  It used to run
before the branch's grammar, so a bare ``/register`` asked the operator to
approve a dangerous command and then printed usage, and ``/todo bogus``,
``/fact forget`` and ``/mcp bogus`` were refused as destructive instead of
being told how to type them.  A line the branch can only answer with usage
now gets that usage without touching the gate.
"""
import pytest

import server
import sonder_runtime.interfaces.repl.repl as sonder_repl


@pytest.fixture(autouse=True)
def _inject_legacy_runtime(monkeypatch):
    monkeypatch.setattr(sonder_repl, "_legacy_runtime", None)
    sonder_repl.configure_legacy_runtime(server)


USAGE_LINES = [
    ("/register", "usage: /register <username> <password>"),
    ("/register onlyname", "usage: /register <username> <password>"),
    ("/run abc", "usage: /run [seconds]"),
    ("/runwindow abc", "usage: /run [seconds]"),
    ("/train many", "usage: /train [N]"),
    ("/fact", "usage: /fact <text> | /fact forget <id> confirm"),
    ("/fact forget", "usage: /fact forget <id> confirm"),
    ("/fact forget 12", "usage: /fact forget <id> confirm"),
    ("/todo bogus", "usage: /todo [list]"),
    ("/todo done", "usage: /todo done <task-id>"),
    ("/todo delete", "usage: /todo delete <task-id>"),
    ("/todo plan only-a-title", "usage: /todo plan <title> | <step>"),
    ("/todo depend a", "usage: /todo depend <task-id> <depends-on-id>"),
    ("/mcp bogus", "usage: /mcp [status|refresh|help]  (unknown MCP action 'bogus')"),
    ("/write only-a-path", "usage: /write <path> <text>"),
    ("/edit a.txt|old", "usage: /edit <path>|<old>|<new>"),
    ("/read", "usage: /read <path>"),
    ("/mkdir", "usage: /mkdir <path>"),
    ("/delete", "usage: /delete <path>"),
]


def _drive(monkeypatch, lines, gate):
    feed = iter(tuple(lines) + ("/exit",))
    monkeypatch.setattr(sonder_repl, "_read_input", lambda *_a, **_k: next(feed))
    monkeypatch.setattr(sonder_repl, "_startup_banner", lambda *_args: "")
    monkeypatch.setattr(sonder_repl, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(sonder_repl, "_named_command_gate", gate)
    monkeypatch.setattr(sonder_repl.command_router, "resolve", lambda _line: None)
    monkeypatch.setattr(
        sonder_repl.server, "sonder",
        lambda *_a, **_k: pytest.fail("a slash command must not run a model turn"),
    )
    sonder_repl.main()


@pytest.mark.parametrize("line,usage", USAGE_LINES)
def test_malformed_lines_get_usage_without_reaching_the_gate(
        monkeypatch, capsys, line, usage):
    def gate(cmd, argument=""):
        if cmd != "/exit":
            pytest.fail("usage-only line %r reached the gate" % line)
        return True, ""

    _drive(monkeypatch, (line,), gate)

    out = capsys.readouterr().out
    assert usage in out
    assert "refused" not in out


@pytest.mark.parametrize("line,usage", [
    item for item in USAGE_LINES if not item[0].startswith("/mcp")
])
def test_pre_gate_usage_matches_what_the_branch_itself_prints(line, usage, monkeypatch):
    # The pre-gate table mirrors the branch grammar.  Bypass it and let the
    # (open) gate through: the branch must print the same usage, so the two
    # cannot drift apart silently.
    monkeypatch.setattr(sonder_repl, "_branch_usage_error", lambda *_a: "")
    calls = []
    for name in (
        "admin_register", "task_create", "task_update", "task_show", "task_delete",
        "task_plan", "task_depend", "sonder_forget_fact", "sonder_remember_fact",
        "file_write", "file_edit", "file_read", "file_delete", "directory_create",
    ):
        monkeypatch.setattr(
            sonder_repl.server, name,
            lambda *a, _name=name, **k: calls.append(_name) or "called",
        )
    output = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: output.append(" ".join(map(str, a))))

    _drive(monkeypatch, (line,), lambda _cmd, _argument="": (True, ""))

    assert calls == []
    assert any(usage in text for text in output), output


def test_well_formed_lines_still_reach_the_gate(monkeypatch, capsys):
    seen = []

    def gate(cmd, argument=""):
        if cmd == "/exit":
            return True, ""
        seen.append((cmd, argument))
        return False, "refused %s: test gate" % cmd

    _drive(monkeypatch, (
        "/register alice s3cret-value",
        "/todo add ship it",
        "/fact forget 12 confirm",
        "/write a.txt hello",
        "/run 5",
    ), gate)

    assert [cmd for cmd, _arg in seen] == ["/register", "/todo", "/fact", "/write", "/run"]
    assert capsys.readouterr().out.count("refused") == 5


def test_bare_fact_forget_is_not_remembered_as_a_fact(monkeypatch):
    remembered = []
    monkeypatch.setattr(sonder_repl, "_branch_usage_error", lambda *_a: "")
    monkeypatch.setattr(
        sonder_repl.server, "sonder_remember_fact",
        lambda text, **_k: remembered.append(text) or "ok",
    )

    _drive(monkeypatch, ("/fact forget",), lambda _cmd, _argument="": (True, ""))

    assert remembered == []
