"""The permission gate at the app's own dispatch surface.

``sonder_serve._handle_slash`` is the sixth hand-written dispatch chain, and it
is the one the Flutter app talks to. It imported ``permission_modes`` only to
*set* the mode at ``/v1/permission-mode`` and never to enforce one, so after
the gate was wired into the agent path, the loop path, the MCP entry point and
the console's two entries, a shipped ``file_delete: deny`` rule bound at four
surfaces and not at the app's -- and ``plan``, advertised on the app's own mode
chip, did not hold still there.

The pre-existing check on this path (``_dangerous_http_slash`` +
``_developer_authorized``) is an authentication tier, not the permission gate:
it asks *who* is calling, and in the default ``local-open`` deployment the
answer is "anyone who can reach this port" (``sonder_serve.py`` deployment
summary). It has nothing to say about which mode is in force or which rules an
operator wrote.

``interactive=False`` here, exactly like the other protocol callers: an HTTP
request has nobody to prompt, so ``ask`` degrades to ``allow`` and this surface
refuses nothing today that it did not refuse before -- while a ``deny`` rule and
``plan`` refuse, which is the point.
"""
from __future__ import annotations

import ast
import os

import pytest

import command_catalog
import permission_modes as pm
import server
import sonder_repl
import sonder_serve as ts

pytestmark = pytest.mark.unit


class _Exploded(AssertionError):
    """Raised by a tool a refusal was supposed to keep from running."""


def _never_runs(*_args, **_kwargs):
    raise _Exploded("a refused tool was dispatched anyway")


@pytest.fixture(autouse=True)
def mode_sandbox(tmp_path, monkeypatch):
    """A tmp state file and a known starting mode for every test here."""
    monkeypatch.setattr(pm, "_state_path", lambda: str(tmp_path / "mode.json"))
    saved = dict(pm._STATE)
    saved_loaded = pm._LOADED
    with pm._LOCK:
        pm._STATE.update(mode=pm.DEFAULT_MODE, elevated=False, elevation_reason="")
    pm._LOADED = True
    try:
        yield
    finally:
        with pm._LOCK:
            pm._STATE.clear()
            pm._STATE.update(saved)
        pm._LOADED = saved_loaded


@pytest.fixture(autouse=True)
def no_rule(monkeypatch):
    """No per-tool rule unless a test asks for one.

    Without this the gate reads the machine's real ``permissions.json``, so
    what these tests assert would depend on whose laptop they ran on.
    """
    monkeypatch.setattr(pm, "_rule_lookup", lambda _tool: None)


def _rules(action):
    """A rule lookup that answers ``action`` for every tool."""
    return lambda tool: {"pattern": tool, "action": action, "note": "test"}


# --- plan holds still at the surface that displays the mode chip ----------


def test_plan_refuses_the_apps_delete_and_never_calls_the_tool(monkeypatch):
    """The plan's motivating sentence, at the last surface it was still true of.

    ``/delete`` here called ``server.file_delete(dry_run=True)`` with nothing
    else in front of it -- so "the only thing actually stopping a delete is
    that tool's own ``dry_run`` default" was literally true at this line after
    the branch that exists to make it false.
    """
    monkeypatch.setattr(server, "file_delete", _never_runs)
    pm.set_mode(pm.PLAN)

    reply = ts._handle_slash("/delete notes.txt")

    assert reply is not None
    assert reply.startswith("refused /delete:")
    assert "plan" in reply


def test_plan_refuses_a_write_and_an_edit_at_the_app_surface(monkeypatch):
    monkeypatch.setattr(server, "file_write", _never_runs)
    monkeypatch.setattr(server, "file_edit", _never_runs)
    pm.set_mode(pm.PLAN)

    assert ts._handle_slash("/write notes.txt hello").startswith("refused /write:")
    assert ts._handle_slash("/append notes.txt hello").startswith("refused /append:")
    assert ts._handle_slash("/edit notes.txt|a|b").startswith("refused /edit:")


def test_plan_still_lets_the_app_read(monkeypatch):
    """``plan`` is "reads only", not "nothing". A verified read must answer.

    Including the one that explains the gate: an operator refused a write must
    still be able to ask why.
    """
    monkeypatch.setattr(server, "context_health", lambda *a, **k: "CONTEXT OK")
    monkeypatch.setattr(server, "master_status", lambda *a, **k: "AGENTS OK")
    pm.set_mode(pm.PLAN)

    assert ts._handle_slash("/context") == "CONTEXT OK"
    assert ts._handle_slash("/agents") == "AGENTS OK"
    assert not ts._handle_slash("/permissions").startswith("refused")


def test_an_ask_class_read_is_refused_here_exactly_as_it_already_was_elsewhere():
    """The cost of gating this chain, stated rather than discovered.

    ``sonder_stats`` is read-only by inspection but the catalog classes it
    ``ask`` -- it is one of the read-only tools sitting in the fail-closed
    default -- so ``plan`` refuses it. That is a pre-existing classification
    gap, not something this gate invented: the console and the MCP entry point
    have refused the same call since they were gated. Pinning it here keeps
    the three surfaces answering alike, so a future fix to the classification
    moves all three at once instead of leaving this one behind.
    """
    assert pm.risk_of("sonder_stats") == "ask"
    pm.set_mode(pm.PLAN)

    assert ts._handle_slash("/stats").startswith("refused /stats:")
    assert not sonder_repl._named_command_gate("/stats")[0]


# --- an explicit deny binds here too --------------------------------------


def test_a_deny_rule_binds_at_the_app_surface_in_every_mode(monkeypatch):
    """The constraint this whole branch turns on: an explicit deny always wins.

    ``file_delete: deny`` is a *shipped default rule*. It bound at four
    surfaces and not at the app's.
    """
    monkeypatch.setattr(server, "file_delete", _never_runs)
    monkeypatch.setattr(pm, "_rule_lookup", _rules("deny"))

    for mode in (pm.MANUAL, pm.ACCEPT_EDITS, pm.AUTO):
        pm.set_mode(mode)
        reply = ts._handle_slash("/delete notes.txt")
        assert reply.startswith("refused /delete:"), mode
        assert "rule denies this tool" in reply, mode


# --- and nothing that worked yesterday starts failing ---------------------


def test_manual_refuses_nothing_at_the_app_surface(monkeypatch):
    """An HTTP caller has nobody to prompt, so ``ask`` degrades to ``allow``.

    This is the "preserve current behaviour" half. If gating this chain made
    the default mode start refusing app requests, the fix would be worse than
    the defect.
    """
    written = []
    monkeypatch.setattr(
        server, "file_write",
        lambda **kwargs: written.append(kwargs) or "wrote it",
    )
    pm.set_mode(pm.MANUAL)

    assert ts._handle_slash("/write notes.txt hello") == "wrote it"
    assert len(written) == 1


def test_the_app_surface_never_prompts(monkeypatch):
    """There is no console here, so nothing may try to read one."""
    import builtins

    monkeypatch.setattr(
        builtins, "input",
        lambda *_a, **_k: pytest.fail("the HTTP gate tried to prompt"),
    )
    monkeypatch.setattr(server, "file_write", lambda **_k: "wrote it")
    pm.set_mode(pm.MANUAL)

    assert ts._handle_slash("/write notes.txt hello") == "wrote it"


def test_a_command_that_fronts_no_tool_is_not_gated(monkeypatch):
    """``/help`` runs no tool, so the gate has nothing to say about it."""
    pm.set_mode(pm.PLAN)

    reply = ts._handle_slash("/help")

    assert reply is not None
    assert not reply.startswith("refused")


def test_a_non_slash_prompt_is_still_not_a_command():
    assert ts._handle_slash("what is the weather like") is None


# --- the delegated branches are gated as what they delegate to ------------


def test_a_control_command_delegation_is_gated_as_the_tool_it_reaches(monkeypatch):
    """``/mkdir`` is named only inside ``server.control_command``.

    Ten of this chain's branches are one-line forwards to
    ``control_command``. Gating only the branches that name a tool *here*
    would leave those ten exactly as ungated as the whole chain was.
    """
    monkeypatch.setattr(server, "directory_create", _never_runs)
    pm.set_mode(pm.PLAN)

    assert "directory_create" in command_catalog.http_slash_tools().get("/mkdir", ())
    assert ts._handle_slash("/mkdir newdir").startswith("refused /mkdir:")


def test_plan_refuses_selfmod_at_the_app_surface(monkeypatch):
    """``/selfmod deploy`` rewrites Sonder's own source; ``plan`` must refuse it."""
    monkeypatch.setattr(server, "control_command", _never_runs)
    pm.set_mode(pm.PLAN)

    reply = ts._handle_slash("/selfmod deploy run-1")

    assert reply.startswith("refused /selfmod:")


# --- coverage of this chain is derived, not hand-listed --------------------


_MCP_TOOL_NAMES = frozenset(
    tool.name for tool in server.mcp._tool_manager.list_tools()
)


def _tools_called_anywhere_in(path, function):
    """Every registered MCP tool called anywhere inside one function.

    Flat on purpose -- no branch attribution and no helper following -- so it
    is an independent check on the map ``command_catalog`` derives.
    """
    with open(os.path.join(os.path.dirname(server.__file__), path),
              encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    scope = next(
        (node for node in ast.walk(tree)
         if isinstance(node, ast.FunctionDef) and node.name == function),
        None,
    )
    assert scope is not None, "%s.%s no longer exists" % (path, function)
    found = set()
    for node in ast.walk(scope):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if isinstance(target, ast.Attribute) and target.attr in _MCP_TOOL_NAMES:
            found.add(target.attr)
        elif isinstance(target, ast.Name) and target.id in _MCP_TOOL_NAMES:
            found.add(target.id)
    return found


def test_every_non_read_tool_the_app_chain_calls_is_covered_by_the_map():
    """No tool this chain can reach may be missing from the gate's map.

    Same shape as the console's coverage test, and for the same reason: the
    map is derived from source so that a branch added tomorrow is covered the
    day it is written, and this is the check that says so.
    """
    covered = {
        tool
        for tools in command_catalog.http_slash_tools().values()
        for tool in tools
    }
    called = _tools_called_anywhere_in("sonder_serve.py", "_handle_slash")

    risky = {tool for tool in called if pm.risk_of(tool) != "safe"}
    assert risky, "the walk found no risky tool at all -- it is broken"
    assert risky - covered == set()


def test_the_gate_sits_in_front_of_every_branch_in_the_chain():
    """Structural: the choke point must precede the first ``cmd ==`` branch.

    ``_handle_slash`` is a flat chain of ~130 ``if cmd == ...`` returns. A gate
    placed after even one of them leaves that one ungated, and no behavioural
    test of the other 129 would notice.
    """
    with open(os.path.join(os.path.dirname(server.__file__), "sonder_serve.py"),
              encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    scope = next(
        (node for node in ast.walk(tree)
         if isinstance(node, ast.FunctionDef) and node.name == "_handle_slash"),
        None,
    )
    assert scope is not None, "sonder_serve._handle_slash no longer exists"

    gate_calls = [
        node.lineno for node in ast.walk(scope)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "_http_slash_refusal"
    ]
    branch_tests = [
        node.lineno for node in ast.walk(scope)
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name)
        and node.left.id == "cmd"
    ]

    assert gate_calls, "_handle_slash no longer consults the permission gate"
    assert branch_tests, "the walk found no dispatch branch -- it is broken"
    assert min(gate_calls) < min(branch_tests), (
        "the gate runs after a dispatch branch, so that branch is ungated"
    )
