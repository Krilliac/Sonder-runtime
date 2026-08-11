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


def test_a_verified_read_is_allowed_here_exactly_as_elsewhere():
    """A verified runtime observation is safe across every surface.

    ``sonder_stats`` is read-only by execution, so plan mode may inspect it;
    the console and HTTP entry points must agree with the catalog.
    """
    assert pm.risk_of("sonder_stats") == "safe"
    pm.set_mode(pm.PLAN)

    assert not ts._handle_slash("/stats").startswith("refused /stats:")
    assert sonder_repl._named_command_gate("/stats")[0]


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


# --- the catalogued fall-through: same surface, other spelling -------------
#
# The branch chain above is only the curated slice. `_dispatch_catalogued_tool`
# runs ANY registered tool by its own name, which is how the app reaches the
# whole surface without a branch per tool -- and it sits after the chain, so a
# gate at the top of the chain does not cover it. Every test above enters
# through a `cmd ==` branch, which is exactly why none of them could see this.


def test_the_catalogued_fall_through_is_gated_by_tool_name(monkeypatch):
    """`/delete` was refused under `plan` while `/file_delete` ran.

    And it ran with the argument the caller chose: `dry_run=false` turns the
    tool's own last-resort default off, so the plan's motivating sentence --
    "the only thing actually stopping a delete is that tool's own `dry_run`
    default" -- was not merely still true here, it was defeated outright.
    """
    monkeypatch.setattr(server, "file_delete", _never_runs)
    pm.set_mode(pm.PLAN)

    reply = ts._handle_slash("/file_delete path=notes.txt dry_run=false")

    assert reply is not None
    assert reply.startswith("refused /file_delete:")


def test_a_deny_rule_binds_on_the_fall_through_too(monkeypatch):
    """`manual` + a `file_delete: deny` rule: the console refused, the app ran it."""
    monkeypatch.setattr(server, "file_delete", _never_runs)
    monkeypatch.setattr(pm, "_rule_lookup", _rules("deny"))
    pm.set_mode(pm.MANUAL)

    reply = ts._handle_slash("/file_delete path=notes.txt")

    assert reply.startswith("refused /file_delete:")
    assert "rule denies this tool" in reply


def test_both_spellings_of_one_tool_get_the_same_answer(monkeypatch):
    """The test that would have caught it, stated as the property it is.

    A surface where `/delete` and `/file_delete` disagree is not a gate, it is
    a spelling test. Asserting the *agreement* -- in both directions, mode by
    mode -- rather than either half is what survives someone adding a third
    way to reach the same tool.

    Note that agreement is not the same as refusal: with no rule in play,
    `acceptEdits` and `auto` let a `dangerous` tool through for a caller with
    nobody to ask, and both spellings must agree about *that* too. A version
    of this test that asserted "both refuse" would have been asserting the
    over-correction instead of the property.
    """
    ran = []
    monkeypatch.setattr(
        server, "file_delete", lambda **kwargs: ran.append(kwargs) or "deleted",
    )

    for mode in pm.MODES:
        pm.set_mode(mode)
        before = len(ran)
        named = ts._handle_slash("/delete notes.txt")
        middle = len(ran)
        by_tool = ts._handle_slash("/file_delete path=notes.txt")
        after = len(ran)

        assert named.startswith("refused") == by_tool.startswith("refused"), (
            "%s: /delete says %r and /file_delete says %r" % (mode, named, by_tool)
        )
        assert (middle - before) == (after - middle), (
            "%s: one spelling reached the tool and the other did not" % mode
        )

    # ...and the guard against both halves being vacuously permissive: at
    # least one mode must actually have refused, and one must have run it.
    assert ran, "no mode ran the tool -- the agreement is vacuous"


def test_the_fall_through_still_runs_a_read_in_every_mode(monkeypatch):
    """The over-correction guard: gating it must not close the whole surface."""
    monkeypatch.setattr(server, "context_health", lambda **_k: "CONTEXT OK")

    for mode in pm.MODES:
        pm.set_mode(mode)
        assert ts._handle_slash("/context_health") == "CONTEXT OK", mode


def test_the_fall_through_refuses_when_the_catalog_is_blind(monkeypatch):
    """Resolving the tool is itself a catalog read; a blind catalog must refuse.

    `parse_invocation` raised `CatalogUnavailable` straight through the
    `except ValueError` here, so a blind registry turned a slash line into an
    unhandled `RuntimeError` on this path instead of a refusal.
    """
    class _Broken:
        def list_tools(self):
            raise RuntimeError("registry not initialised")

    command_catalog.reset_cache()
    monkeypatch.setattr(server.mcp, "_tool_manager", _Broken())
    try:
        reply = ts._handle_slash("/file_delete path=notes.txt")
        assert reply is not None
        assert reply.startswith("refused")
        assert "registry could not be read" in reply
    finally:
        monkeypatch.undo()
        command_catalog.reset_cache()



def test_the_window_runner_is_refused_at_the_app_surface_too(monkeypatch):
    """`/run` was refused under `plan` here while `/runwindow` launched a console."""
    pm.set_mode(pm.PLAN)

    for name in ("/runwindow", "/runnew", "/runconsole"):
        reply = ts._handle_slash("%s 30" % name)
        assert reply is not None, name
        assert reply.startswith("refused %s:" % name), (name, reply)


def test_the_wrapper_backed_writes_are_refused_here_as_at_the_console(monkeypatch):
    """`/emotion` and `/prefer` write, and this surface resolved them to nothing.

    Their branches here call `server.emotion_command` / `preference_command`
    -- wrappers -- while the console branches call the tools directly. Same
    command, same write, refused for the operator and allowed for the app.
    """
    monkeypatch.setattr(server, "emotion_command", _never_runs)
    monkeypatch.setattr(server, "preference_command", _never_runs)
    pm.set_mode(pm.PLAN)

    for name in ("/emotion", "/emotions", "/mood", "/vectors"):
        assert ts._handle_slash("%s joy=0.8" % name).startswith("refused %s:" % name), name
    for name in ("/prefer", "/preference", "/preferences"):
        assert ts._handle_slash("%s terse" % name).startswith("refused %s:" % name), name
