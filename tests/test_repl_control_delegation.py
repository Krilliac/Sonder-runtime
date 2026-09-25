"""Every ``server.control_command`` branch the catalog advertises reaches it.

The console catalog lists the union of the REPL's own branches and
``server.control_command``'s, and the console gate grades a command by that
chain's tools.  ``/mission`` and ``/vision`` (``/analyzeimage``) were listed
and gated -- an operator was asked "run /mission?" -- but the REPL never
forwarded them, so an approved command answered "unknown command".
"""
import ast

import pytest

import server
import sonder_runtime.interfaces.repl.repl as sonder_repl


@pytest.fixture(autouse=True)
def _inject_legacy_runtime(monkeypatch):
    monkeypatch.setattr(sonder_repl, "_legacy_runtime", None)
    sonder_repl.configure_legacy_runtime(server)


def _drive(monkeypatch, lines):
    feed = iter(tuple(lines) + ("/exit",))
    monkeypatch.setattr(sonder_repl, "_read_input", lambda *_a, **_k: next(feed))
    monkeypatch.setattr(sonder_repl, "_startup_banner", lambda *_args: "")
    monkeypatch.setattr(sonder_repl, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(
        sonder_repl, "_named_command_gate", lambda _cmd, _argument="": (True, ""),
    )
    monkeypatch.setattr(
        sonder_repl.server, "sonder",
        lambda *_a, **_k: pytest.fail("a slash command must not run a model turn"),
    )
    sonder_repl.main()


def _branch_names(path, function):
    tree = ast.parse(open(path, encoding="utf-8").read())
    scope = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == function
    )
    names = set()
    for node in ast.walk(scope):
        if (
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Name)
            and node.left.id == "cmd"
        ):
            comparator = node.comparators[0]
            items = (
                comparator.elts
                if isinstance(comparator, (ast.Tuple, ast.List, ast.Set))
                else [comparator]
            )
            names.update(
                item.value for item in items
                if isinstance(item, ast.Constant)
                and isinstance(item.value, str)
                and item.value.startswith("/")
                and len(item.value) > 1
            )
    return names


def test_every_control_command_branch_has_a_repl_branch():
    served = _branch_names(server.__file__, "control_command")
    console = _branch_names(sonder_repl.__file__, "main")

    assert served - console == set()


@pytest.mark.parametrize("line", [
    "/mission",
    "/mission status",
    "/vision photo.png | what is this",
    "/analyzeimage photo.png | what is this",
])
def test_catalogued_control_commands_are_forwarded(monkeypatch, capsys, line):
    seen = []

    def _control(prompt, **kwargs):
        seen.append((prompt, kwargs))
        return "handled %s" % prompt

    monkeypatch.setattr(sonder_repl.server, "control_command", _control)

    _drive(monkeypatch, (line,))

    out = capsys.readouterr().out
    assert "unknown command" not in out
    assert [prompt for prompt, _ in seen] == [line]
    assert seen[0][1]["project"] == server.DEFAULT_PROJECT
    assert "handled %s" % line in out
