"""The permission gate at the two dispatch sites that actually run tools.

``permission_modes.decide()`` was a pure function with no production callers:
a well-tested decision point that decided nothing. These tests cover the two
call sites that opt into it -- ``server._agent_dispatch`` (the agent /
workbench / autopilot path) and ``sonder_repl._run_catalogued`` (``/<tool>``
typed at the console) -- and, just as importantly, pin down what it must
*not* start refusing.

The load-bearing assertion in this file is
``test_manual_refuses_nothing_the_mode_did_not_refuse_before``: mode ``manual``
is the default, so if wiring the gate in makes it stop anything the mode
itself did not already stop, Sonder starts refusing work that has always
worked. Rule-level ``deny`` is the deliberate exception -- enforcing the
per-tool rules ``/permissions`` has always printed is the point of the change.
"""
from __future__ import annotations

import ast
import asyncio
import builtins
import io
import os
import sys

import pytest
from mcp.server.fastmcp.exceptions import ToolError

import command_catalog
import permission_modes as pm
import permission_rules
import server
import sonder_repl
import reloadable_mcp
import sonder_serve
from sonder_runtime.domain.execution import policy as execution_policy

pytestmark = pytest.mark.unit


class _Exploded(AssertionError):
    """Raised by a tool that a refusal was supposed to prevent from running."""


def _never_runs(*_args, **_kwargs):
    raise _Exploded("a refused tool was dispatched anyway")


@pytest.fixture(scope="module", autouse=True)
def _guard_process_state():
    """Fail this module if it leaks ``permission_modes`` state into other suites."""
    before_state = dict(pm._STATE)
    before_loaded = pm._LOADED
    before_path = pm._state_path
    before_rule_lookup = pm._rule_lookup
    yield
    assert dict(pm._STATE) == before_state, (
        "permission_modes._STATE leaked out of this module: %r -> %r"
        % (before_state, dict(pm._STATE))
    )
    assert pm._LOADED == before_loaded, "permission_modes._LOADED leaked"
    assert pm._state_path is before_path, "_state_path monkeypatch was not undone"
    assert pm._rule_lookup is before_rule_lookup, "_rule_lookup monkeypatch leaked"


@pytest.fixture(autouse=True)
def mode_sandbox(tmp_path, monkeypatch):
    """A tmp state file and a known starting mode for every test here.

    ``set_mode`` persists, so without redirecting ``_state_path`` a test that
    switches to ``auto`` would write that choice into the shared test home and
    change what every later test file decides.
    """
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
            pm._STATE.update(saved)
        pm._LOADED = saved_loaded


class _FakeTty(io.StringIO):
    """A stdin that claims to be a terminal, so the console path stays live."""

    def isatty(self):
        return True


@pytest.fixture(autouse=True)
def console_has_an_operator(monkeypatch):
    """Every test here runs as if a person were at the console, unless it says so.

    The console gate asks whether stdin is a terminal to decide whether `ask`
    means a prompt or degrades to allow. Under pytest stdin is captured and is
    never a tty, so without this the console tests would silently exercise the
    *piped* path: their prompts would stop being prompts and they would pass
    while testing nothing. Patching the stream rather than the helper keeps the
    real `_console_has_operator` in the path being tested.
    """
    monkeypatch.setattr(sys, "stdin", _FakeTty())


@pytest.fixture(autouse=True)
def no_console_prompt(monkeypatch):
    """A test that reaches ``input()`` unintentionally must fail, not hang."""
    monkeypatch.setattr(
        builtins, "input",
        lambda *_a, **_k: pytest.fail("the gate prompted without being asked to"),
    )


def _rules(action):
    """A rule lookup that answers ``action`` for every tool."""
    return lambda tool: {"pattern": tool, "action": action, "note": "test"}


_MCP_TOOL_NAMES = frozenset(
    tool.name for tool in server.mcp._tool_manager.list_tools()
)


def _tools_called_anywhere_in(path, function):
    """Every registered MCP tool called anywhere inside one function.

    Flat on purpose: no branch attribution, no helper following. That makes it
    an independent check on `command_catalog._branch_tool_calls`, whose whole
    job is the attribution this deliberately does not attempt.
    """
    with open(os.path.join(os.path.dirname(server.__file__), path), encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    scope = next(
        (
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == function
        ),
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


# --- the agent / workbench / autopilot path -------------------------------


def test_plan_mode_refuses_a_mutating_dispatch_and_never_calls_the_tool(monkeypatch):
    monkeypatch.setattr(server, "file_write", _never_runs)
    pm.set_mode(pm.PLAN)

    observation = server._agent_dispatch("file_write", {"path": "x", "content": "y"})

    assert observation.startswith("ERROR: HOST POLICY:")
    assert "mode=plan" in observation
    assert "file_write" in observation


def test_plan_mode_still_lets_reads_through(monkeypatch):
    monkeypatch.setattr(server, "file_read", lambda **_kwargs: "contents")
    pm.set_mode(pm.PLAN)

    assert server._agent_dispatch("file_read", {"path": "x"}) == "contents"


def test_auto_mode_runs_the_dispatch_plan_refused(monkeypatch):
    monkeypatch.setattr(server, "file_write", lambda **_kwargs: "wrote it")
    pm.set_mode(pm.AUTO)

    assert server._agent_dispatch("file_write", {"path": "x", "content": "y"}) == "wrote it"


def test_execution_tools_flow_in_auto_and_stop_in_plan(monkeypatch):
    monkeypatch.setattr(server, "run_code", lambda **_kwargs: "ran")
    pm.set_mode(pm.AUTO)
    assert server._agent_dispatch("run_code", {"code": "print(1)"}) == "ran"

    monkeypatch.setattr(server, "run_code", _never_runs)
    pm.set_mode(pm.PLAN)
    assert server._agent_dispatch("run_code", {"code": "print(1)"}).startswith(
        "ERROR: HOST POLICY:"
    )


def test_an_explicit_deny_rule_stops_the_agent_path_in_every_mode(monkeypatch):
    monkeypatch.setattr(server, "file_write", _never_runs)
    monkeypatch.setattr(pm, "_rule_lookup", _rules(pm.DENY))

    for mode in pm.MODES:
        pm.set_mode(mode)
        observation = server._agent_dispatch(
            "file_write", {"path": "x", "content": "y"},
        )
        assert observation.startswith("ERROR: HOST POLICY:"), mode
        assert "rule denies" in observation, mode


def test_the_refusal_reads_as_a_failed_step_the_loop_understands(monkeypatch):
    """The loop's success predicate must classify a gate refusal as a failure.

    A refusal the loop reads as a *successful* observation would be recorded
    as evidence and could satisfy a required-tool gate, which is the quiet
    way a policy stops meaning anything.
    """
    monkeypatch.setattr(server, "file_write", _never_runs)
    pm.set_mode(pm.PLAN)

    observation = server._agent_dispatch("file_write", {"path": "x", "content": "y"})

    assert not server._agent_tool_observation_ok("file_write", observation)


def test_an_alias_is_gated_under_its_canonical_name(monkeypatch):
    """``/assetgen`` must not be a way around a rule written for its real name."""
    monkeypatch.setattr(server, "artifact_generate", _never_runs)
    monkeypatch.setattr(
        pm, "_rule_lookup",
        lambda tool: {"pattern": tool, "action": pm.DENY} if tool == "artifact_generate" else None,
    )
    pm.set_mode(pm.AUTO)

    observation = server._agent_dispatch("assetgen", {"name": "x", "brief": "y"})

    assert observation.startswith("ERROR: HOST POLICY:")
    assert "artifact_generate" in observation


def test_the_gate_does_not_replace_the_read_only_policy(monkeypatch):
    """``auto`` allows the mode's part; the read-only filter still refuses."""
    monkeypatch.setattr(server, "file_write", _never_runs)
    pm.set_mode(pm.AUTO)

    observation = server._agent_dispatch(
        "file_write", {"path": "x", "content": "y"}, read_only=True,
    )

    assert observation.startswith("ERROR:")
    assert "HOST POLICY: tool 'file_write' is refused by the active" not in observation


# --- the loop / workflow_run path -----------------------------------------
#
# The third place a *model* chooses what runs: `loop` and `workflow_run`
# execute model-authored actions, `file_delete` and `workspace_run` among them.


def test_plan_refuses_a_loop_action_and_never_runs_it(monkeypatch):
    monkeypatch.setattr(server.code_runner, "run_code", _never_runs)
    pm.set_mode(pm.PLAN)

    result = server._loop_dispatch({"type": "code", "code": "print(1)"})

    assert result["ok"] is False
    assert "permission gate" in result["output"]
    assert "mode=plan" in result["output"]


def test_a_loop_action_alias_is_gated_as_the_tool_it_runs():
    """`type: "code"` runs `run_code`; the gate must decide on that, not "code"."""
    assert server._loop_action_tool("code") == "run_code"
    assert server._loop_action_tool("project") == "run_project"
    assert server._loop_action_tool("work") == "workbench_agent"
    assert server._loop_action_tool("assetgen") == "artifact_generate"
    assert server._loop_action_tool("file_delete") == "file_delete"


def _loop_action_branches():
    """`{action_type: {registered tools its branch calls}}` for `_loop_dispatch`.

    Read out of the source so the hand-written `_LOOP_ACTION_TOOLS` table
    cannot quietly stop covering the branches it exists for.
    """
    with open(server.__file__, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    scope = next(
        (
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_loop_dispatch"
        ),
        None,
    )
    assert scope is not None, "server._loop_dispatch no longer exists"
    branches = {}
    for node in ast.walk(scope):
        if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
            continue
        left = node.test.left
        if not isinstance(left, ast.Name) or left.id != "action_type":
            continue
        comparator = node.test.comparators[0]
        elements = (
            comparator.elts
            if isinstance(comparator, (ast.Tuple, ast.List, ast.Set))
            else [comparator]
        )
        called = set()
        for child in ast.walk(ast.Module(body=node.body, type_ignores=[])):
            if not isinstance(child, ast.Call):
                continue
            target = child.func
            if isinstance(target, ast.Name) and target.id in _MCP_TOOL_NAMES:
                called.add(target.id)
            elif isinstance(target, ast.Attribute) and target.attr in _MCP_TOOL_NAMES:
                called.add(target.attr)
        for element in elements:
            if isinstance(element, ast.Constant) and isinstance(element.value, str):
                branches.setdefault(element.value, set()).update(called)
    return branches


def test_every_loop_action_that_runs_a_tool_is_gated_as_that_tool():
    """`_LOOP_ACTION_TOOLS` is hand-written; this is what stops it going stale.

    A loop action whose name is not a tool name and is not translated to one
    is decided on a name `risk_of` has never heard of. Non-interactively that
    is nearly indistinguishable from allow -- only `plan` refuses it -- and a
    `deny` rule written for the tool it actually runs would not match, which
    is a silent hole rather than a visible one. Actions that run no tool at
    all (`sleep`) are fine to leave unresolved; actions that run one are not.
    """
    unresolved = {
        action: sorted(tools)
        for action, tools in _loop_action_branches().items()
        if tools and server._loop_action_tool(action) not in _MCP_TOOL_NAMES
    }

    assert unresolved == {}, (
        "these loop actions run tools but the gate resolves them to a "
        "non-tool name; add them to server._LOOP_ACTION_TOOLS: %r" % unresolved
    )


def test_a_deny_rule_stops_a_loop_action_in_every_mode(monkeypatch):
    monkeypatch.setattr(pm, "_rule_lookup", _rules(pm.DENY))

    for mode in pm.MODES:
        pm.set_mode(mode)
        result = server._loop_dispatch({"type": "file_delete", "path": "x"})
        assert result["ok"] is False, mode
        assert "rule denies" in result["output"], mode


def test_manual_leaves_the_loop_path_running(monkeypatch):
    monkeypatch.setattr(
        server.code_runner, "run_code",
        lambda **_kwargs: {"ok": True, "returncode": 0, "stdout": "1", "stderr": ""},
    )
    pm.set_mode(pm.MANUAL)

    result = server._loop_dispatch({"type": "code", "code": "print(1)"})

    assert result["ok"] is True


# --- selfmod ---------------------------------------------------------------


def test_selfmod_gets_no_exemption_from_the_delete_rule():
    """The self-editing agent is gated like everything else. Deliberately.

    `_execute_selfmod_run`'s tool allowlist includes `file_delete`, and it
    routes through `_agent_dispatch`, so the built-in `file_delete: deny` rule now
    refuses those deletes. Exempting it would invert the risk ordering: the
    least-supervised actor on the machine -- a model rewriting Sonder's own
    source unattended -- would be the one actor the operator's only
    ship-by-default denial did not apply to. The capability is still
    available, as one explicit, auditable, written-down grant
    (`permission_rule_set file_delete allow`), which is the right shape for
    that decision. `file_write`/`file_edit` are unaffected, so scoped edits
    still work.
    """
    pm.set_mode(pm.MANUAL)
    allowlist = _selfmod_agent_allowlist()

    assert "file_delete" in allowlist, "the premise of this test moved"
    assert server._agent_permission_gate_error("file_delete").startswith(
        "ERROR: HOST POLICY:"
    )
    for tool in sorted(allowlist - {"file_delete"}):
        assert server._agent_permission_gate_error(tool) == "", tool


def _selfmod_agent_allowlist():
    """The tool allowlist `_execute_selfmod_run` hands its editing agent.

    Read out of the source rather than imported, because the allowlist is a
    literal inside the call and there is nothing to import. That makes a
    rename a silent no-match, so the miss is asserted rather than left to
    surface as a `StopIteration` from a bare `next()` -- which is what it did
    when this helper was first written against a function name
    (`_selfmod_execute`) that does not exist.
    """
    with open(server.__file__, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    scope = next(
        (
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "_execute_selfmod_run"
        ),
        None,
    )
    assert scope is not None, "server._execute_selfmod_run no longer exists"
    for node in ast.walk(scope):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg == "tool_allowlist" and isinstance(keyword.value, ast.Set):
                return {
                    item.value for item in keyword.value.elts
                    if isinstance(item, ast.Constant)
                }
    raise AssertionError("_execute_selfmod_run no longer passes a tool_allowlist")


# --- the constraint this whole change has to satisfy ----------------------


def test_manual_refuses_nothing_the_mode_did_not_refuse_before():
    """Default mode must not start denying tools that worked yesterday.

    Wiring the gate in flips two dormant layers on at once. The mode layer was
    a no-op on this path by construction (``interactive=False`` degrades
    ``ask`` to ``allow``), so the refusals mode ``manual`` may produce are the
    per-tool ``deny`` rules that ``/permissions`` has always printed and never
    enforced. Anything else refused here is a regression that would surface to
    a user as "Sonder stopped working".

    Task #57 adds the second, declared exception:
    ``permission_modes.DURABLE_AUTHORITY_TOOLS``, the class the non-interactive
    degrade deliberately does not apply to. It is admitted here as an
    enumerated set rather than by loosening the comparison, so the floor still
    binds for every other name -- a change that starts denying anything else
    still fails this test.

    The cost this test exists to catch is measured, not assumed: none of those
    names is reachable from ``_agent_dispatch`` at all (asserted below), so no
    agent run that worked yesterday stops working. The gate refuses them on
    this path anyway, and deliberately -- a model must not be able to grant
    itself authority, and "there is no dispatch branch today" is not a
    guarantee about tomorrow.
    """
    import tool_capabilities

    pm.set_mode(pm.MANUAL)
    # The hermetic test home has no permissions.json, so the gate's real rule
    # lookup resolves against exactly these built-in defaults.
    defaults = permission_rules.DEFAULT_RULES

    refused = set()
    denied_by_rule = set()
    for command in command_catalog.catalog():
        # Compare on the canonical name the gate itself decides on, so an
        # alias is not counted as a mismatch with the rule it resolves to.
        tool = server._canonical_agent_tool_name(command.name.lstrip("/"))
        if server._agent_permission_gate_error(tool):
            refused.add(tool)
        if execution_policy.evaluate(defaults, tool)["action"] == pm.DENY:
            denied_by_rule.add(tool)

    assert refused == denied_by_rule | pm.DURABLE_AUTHORITY_TOOLS
    # And the default policy really does deny something, so an empty set on
    # both sides can never pass this vacuously.
    assert "file_delete" in denied_by_rule
    # The whole justification for admitting the class above: it costs the agent
    # lane nothing, because the agent lane cannot call any of it.
    dispatchable = set(tool_capabilities.dispatch_names(server._agent_dispatch))
    assert pm.DURABLE_AUTHORITY_TOOLS & dispatchable == set()


def test_manual_allows_every_risk_class_on_the_agent_path():
    """Spelled out per class, so a matrix change cannot slip past unnoticed."""
    pm.set_mode(pm.MANUAL)

    for tool in ("status", "task_plan", "file_write", "run_code"):
        assert server._agent_permission_gate_error(tool) == "", tool


def test_manual_leaves_the_direct_mcp_surface_alone():
    """The default mode must not start refusing MCP clients that always worked."""
    pm.set_mode(pm.MANUAL)

    blocks, _ = asyncio.run(
        server.mcp.call_tool("task_create", {"title": "gate probe"}),
    )

    assert "task created" in blocks[0].text


def test_plan_denies_a_direct_mcp_client_call():
    """``plan`` says "reads only - no writes, no commands"; it must mean it.

    ``permission_modes`` documents that ``ask`` degrades to ``allow`` for a
    non-interactive caller "except under ``plan``, which denies everywhere".
    An operator who selects ``plan`` and then watches an MCP client mutate the
    workspace has been lied to by the mode indicator, so the protocol entry
    point honours it too.
    """
    pm.set_mode(pm.PLAN)

    with pytest.raises(ToolError) as raised:
        asyncio.run(server.mcp.call_tool("task_create", {"title": "gate probe"}))

    assert "permission gate" in str(raised.value)
    assert "mode=plan" in str(raised.value)


def test_plan_still_lets_a_client_read():
    pm.set_mode(pm.PLAN)

    blocks, _ = asyncio.run(server.mcp.call_tool("task_list", {}))

    assert blocks


def test_plan_cannot_trap_a_client_that_selected_it(monkeypatch):
    """The one way back out of ``plan`` must not be refused by ``plan``.

    ``permission_mode`` is risk ``ask``, which ``plan`` denies, and the mode
    persists to disk -- so gating it here left a client that selected ``plan``
    unable to select anything else, across restarts, with no remedy but
    editing ``permission_mode.json`` by hand. The console has always exempted
    the gate's own control for exactly this reason; the protocol surface is
    the same argument with no keyboard attached.
    """
    pm.set_mode(pm.PLAN)

    blocks, _ = asyncio.run(
        server.mcp.call_tool("permission_mode", {"mode": "manual"}),
    )

    assert pm.current_mode() == pm.MANUAL
    assert blocks


def test_the_gate_control_exemption_has_one_definition():
    """Two copies of a security-relevant set is how one of them goes stale."""
    assert pm.GATE_CONTROL_TOOLS == frozenset({"permission_mode"})
    assert sonder_repl.GATE_EXEMPT_TOOLS is pm.GATE_CONTROL_TOOLS


def test_the_agent_path_gets_no_gate_control_exemption():
    """A model must not be able to lift its own restraint."""
    pm.set_mode(pm.PLAN)

    assert server._agent_permission_gate_error("permission_mode").startswith(
        "ERROR: HOST POLICY:"
    )


def test_a_verified_read_is_classified_safe_rather_than_unknown():
    """`plan` denying `task_list` was a misclassification, not a policy.

    `_risk_for` falls back to `ask` for anything the server's policy sets do
    not name -- the correct fail-closed default, and one `plan` denies. It was
    the wrong answer for `task_list`/`task_show`, which run a `SELECT` and
    nothing else: `plan` exists to stop Sonder changing things, not to stop it
    answering a question. Their mutating siblings are asserted alongside so a
    widened read-only set cannot drag them along unnoticed.
    """
    assert pm.risk_of("task_list") == "safe"
    assert pm.risk_of("task_show") == "safe"
    assert pm.risk_of("task_update") == "ask"
    assert pm.risk_of("task_delete") == "dangerous"


def test_an_internal_python_call_is_not_gated_twice():
    """Only the protocol entry point gates; the function itself does not.

    ``_agent_dispatch`` and the REPL each call the tool function directly with
    their own ``interactive`` value. Gating the function bodies as well would
    prompt twice for one console command, and would make the agent path's
    refusal reason the wrong one.
    """
    pm.set_mode(pm.PLAN)

    assert server._agent_dispatch(
        "task_create", {"title": "gate probe"},
    ).startswith("ERROR: HOST POLICY:")
    assert "task created" in server.task_create(title="gate probe")


# --- the console path -----------------------------------------------------


def test_console_asks_before_a_mutating_tool_and_a_no_does_not_run_it(monkeypatch):
    monkeypatch.setattr(server, "file_write", _never_runs)
    asked = []
    monkeypatch.setattr(
        sonder_repl, "_confirm", lambda question: asked.append(question) or False,
    )
    pm.set_mode(pm.MANUAL)

    output = sonder_repl._run_catalogued(
        "/file_write path=x content=y", "/file_write",
    )

    assert output == "skipped /file_write"
    assert len(asked) == 1
    assert "/file_write" in asked[0]


def test_console_runs_the_tool_when_the_operator_says_yes(monkeypatch):
    monkeypatch.setattr(server, "file_write", lambda **_kwargs: "wrote it")
    monkeypatch.setattr(sonder_repl, "_confirm", lambda _question: True)
    pm.set_mode(pm.MANUAL)

    output = sonder_repl._run_catalogued(
        "/file_write path=x content=y", "/file_write",
    )

    assert output == "wrote it"


def test_console_refusal_names_the_reason_and_runs_nothing(monkeypatch):
    monkeypatch.setattr(server, "file_write", _never_runs)
    monkeypatch.setattr(
        sonder_repl, "_confirm",
        lambda _question: pytest.fail("a denial must not be downgraded to a prompt"),
    )
    pm.set_mode(pm.PLAN)

    output = sonder_repl._run_catalogued(
        "/file_write path=x content=y", "/file_write",
    )

    assert output.startswith("refused /file_write:")
    assert "plan" in output


def test_console_never_prompts_for_a_read(monkeypatch):
    monkeypatch.setattr(
        sonder_repl, "_confirm",
        lambda _question: pytest.fail("a read-only tool must not prompt"),
    )
    pm.set_mode(pm.MANUAL)

    assert "sonder task progress" in sonder_repl._run_catalogued(
        "/task_progress", "/task_progress",
    )


def test_the_prompt_defaults_to_no(monkeypatch):
    answers = {"": False, " ": False, "n": False, "no": False, "nope": False,
               "yes": True, "Y": True, " y ": True}
    for typed, expected in answers.items():
        monkeypatch.setattr(builtins, "input", lambda _prompt, t=typed: t)
        assert sonder_repl._confirm("run it?") is expected, repr(typed)


@pytest.mark.parametrize("failure", [EOFError, OSError, KeyboardInterrupt])
def test_a_missing_terminal_is_a_no_not_a_yes(monkeypatch, failure):
    def _explode(_prompt):
        raise failure()

    monkeypatch.setattr(builtins, "input", _explode)

    assert sonder_repl._confirm("run it?") is False


def test_plan_mode_cannot_trap_the_operator_at_the_console(monkeypatch):
    """The gate's own control must never be refused by the gate.

    ``permission_mode`` is risk ``ask``, which ``plan`` denies. Gating it
    would leave whoever is at the keyboard in ``plan`` with no console way
    back out -- a refusal nobody can act on.
    """
    monkeypatch.setattr(
        sonder_repl, "_confirm",
        lambda _question: pytest.fail("the mode control must not prompt either"),
    )
    pm.set_mode(pm.PLAN)

    may_run, refusal = sonder_repl._permission_gate("permission_mode")

    assert may_run and refusal == ""
    assert "auto" in sonder_repl._mode_command("auto")
    assert pm.current_mode() == pm.AUTO


def test_the_mode_command_shows_and_sets(monkeypatch):
    pm.set_mode(pm.MANUAL)

    overview = sonder_repl._mode_command("")
    assert "sonder permission modes" in overview
    assert pm.current_mode() == pm.MANUAL

    assert sonder_repl._mode_command("plan")
    assert pm.current_mode() == pm.PLAN
    assert "unknown mode" in sonder_repl._mode_command("nonsense")
    assert pm.current_mode() == pm.PLAN
    # --explain describes a mode without switching to it.
    assert "destructive" in sonder_repl._mode_command("auto --explain")
    assert pm.current_mode() == pm.PLAN


def test_an_unknown_console_command_still_gets_suggestions_not_a_refusal():
    pm.set_mode(pm.PLAN)

    output = sonder_repl._run_catalogued("/task_prog", "/task_prog")

    assert "unknown command /task_prog" in output


def test_the_console_exemption_set_cannot_widen_silently():
    """One name, and the reason it is there is written down beside it."""
    assert sonder_repl.GATE_EXEMPT_TOOLS == frozenset({"permission_mode"})


# --- the console's ~50 hand-written branches ------------------------------
#
# `_run_catalogued` is only the fallback. `/write`, `/delete`, `/mkdir` and the
# rest call their tool directly, so gating the fallback alone left `plan` --
# advertised as "reads only" -- writing and deleting files.


# (console command, tool it fronts). Aliases and control_command delegations
# included on purpose: `/mkdir` is only named inside server.control_command.
_NAMED_BRANCHES = [
    ("/write", "file_write"),
    ("/append", "file_write"),
    ("/edit", "file_edit"),
    ("/delete", "file_delete"),
    ("/mkdir", "directory_create"),
    ("/runprogram", "workspace_run"),
    ("/runscript", "script_run"),
    ("/scaffold", "scaffold_project"),
    ("/setaccount", "admin_set_account"),
    ("/register", "admin_register"),
    ("/qualityfix", "memory_quality_repair"),
    ("/work", "workbench_agent"),
    ("/game", "game_generate_and_test"),
    ("/asset", "artifact_generate"),
    ("/todo", "task_delete"),
    ("/cot", "admin_private_chain_of_thought"),
]


@pytest.mark.parametrize("command,tool", _NAMED_BRANCHES)
def test_every_named_branch_resolves_to_the_tool_it_runs(command, tool):
    assert tool in command_catalog.console_tools().get(command, ())


@pytest.mark.parametrize("command,tool", _NAMED_BRANCHES)
def test_plan_refuses_every_named_branch_that_is_not_a_read(monkeypatch, command, tool):
    del tool
    monkeypatch.setattr(
        sonder_repl, "_confirm",
        lambda _question: pytest.fail("a plan-mode denial must not prompt"),
    )
    pm.set_mode(pm.PLAN)

    may_run, refusal = sonder_repl._named_command_gate(command)

    assert not may_run
    assert refusal.startswith("refused %s:" % command)


def _rule_denied_tools(command):
    """Tools this branch fronts that the shipped rules deny outright.

    Derived from `permission_rules.DEFAULT_RULES` rather than listed, so a
    rule change moves the expectation instead of breaking a hand-kept copy of
    it. The hermetic test home has no permissions.json, so these defaults are
    exactly what the gate's real lookup resolves against.
    """
    return sorted(
        tool for tool in command_catalog.console_tools().get(command, ())
        if execution_policy.evaluate(
            permission_rules.DEFAULT_RULES, tool,
        )["action"] == pm.DENY
    )


@pytest.mark.parametrize("command,tool", _NAMED_BRANCHES)
def test_manual_prompts_before_every_named_branch_and_a_no_stops_it(
    monkeypatch, command, tool,
):
    """Manual asks first, and a "no" means the branch does not run.

    Two of these never reach the prompt, and must not: `/delete` and `/cot`
    front tools the shipped rules deny outright (`file_delete`,
    `admin_private_chain_of_thought`), and an explicit deny outranks every
    mode -- including manual's ask. Downgrading such a rule to a y/N prompt
    would let one keystroke defeat a written-down denial, so they are
    asserted here as refusals rather than excused from the parametrization.
    """
    del tool
    denied = _rule_denied_tools(command)
    prompted = []
    monkeypatch.setattr(
        sonder_repl, "_confirm", lambda question: prompted.append(question) or False,
    )
    pm.set_mode(pm.MANUAL)

    may_run, refusal = sonder_repl._named_command_gate(command)

    assert not may_run
    if denied:
        assert prompted == [], "a rule denial must not be downgraded to a prompt"
        assert refusal.startswith("refused %s: rule denies this tool" % command)
        assert denied[0] in refusal
    else:
        assert prompted, "manual must ask before a branch that is not a read"
        assert refusal == "skipped %s" % command


def test_the_deny_rule_branches_are_exactly_the_two_the_shipped_rules_name():
    """Keeps the test above from passing vacuously in either direction.

    If `_rule_denied_tools` silently returned nothing -- a broken lookup, a
    dropped default rule -- every branch would take the "prompts" arm and the
    deny-outranks-ask precedence would stop being tested at all.
    """
    denied = {command for command, _ in _NAMED_BRANCHES if _rule_denied_tools(command)}

    assert denied == {"/delete", "/cot"}


def test_a_console_command_that_fronts_no_tool_is_never_gated(monkeypatch):
    """`/help`, `/exit` and friends must keep working in every mode."""
    monkeypatch.setattr(
        sonder_repl, "_confirm",
        lambda _question: pytest.fail("a console command with no tool prompted"),
    )
    pm.set_mode(pm.PLAN)

    for command in ("/help", "/exit", "/trace", "/model", "/persona", "/new"):
        assert sonder_repl._named_command_gate(command) == (True, ""), command


def test_the_mode_control_branch_stays_reachable_in_plan(monkeypatch):
    monkeypatch.setattr(
        sonder_repl, "_confirm",
        lambda _question: pytest.fail("the mode control must not prompt"),
    )
    pm.set_mode(pm.PLAN)

    assert sonder_repl._named_command_gate("/mode") == (True, "")


def test_a_multi_tool_branch_is_gated_at_its_most_dangerous_member(monkeypatch):
    """`/todo` reaches task_list *and* task_delete; the worst one governs.

    Which one runs depends on an argument the gate has not parsed, so it
    rounds toward refusal. The prompt must name the dangerous member, not
    whichever branch ast.walk happened to see first.
    """
    asked = []
    monkeypatch.setattr(
        sonder_repl, "_confirm", lambda question: asked.append(question) or True,
    )
    pm.set_mode(pm.AUTO)  # auto allows everything except dangerous

    may_run, _refusal = sonder_repl._named_command_gate("/todo")

    assert may_run
    assert len(asked) == 1
    assert "dangerous" in asked[0]


# --- a piped console has nobody to prompt ---------------------------------
#
# `sonder < script.txt` and `echo /stats | sonder` are ordinary ways to drive
# the REPL. There is no one at the keyboard, so `input()` does not ask anybody
# anything -- it reads the next line of the script.


def _piped_console(monkeypatch, script):
    """Point stdin at `script` with no terminal behind it, and hand it back."""
    piped = io.StringIO(script)
    monkeypatch.setattr(sys, "stdin", piped)
    return piped


def test_a_piped_console_does_not_eat_the_next_command(monkeypatch):
    """The prompt must not consume the line that follows it.

    Piped, `input()` returns the next command in the script and the gate reads
    it as the answer to a y/N question. `/stats` was skipped because the
    operator "answered" `/exit`, and `/exit` was swallowed on the way past --
    two commands lost to one prompt that nobody saw.
    """
    piped = _piped_console(monkeypatch, "/exit\n")
    pm.set_mode(pm.MANUAL)

    may_run, refusal = sonder_repl._named_command_gate("/stats")

    assert (may_run, refusal) == (True, "")
    assert piped.readline() == "/exit\n", "the next command was eaten by the prompt"


def test_a_piped_console_does_not_silently_deny_a_read_under_manual(monkeypatch):
    """`manual` is the default; piped reads have always worked and must keep working.

    These are the plain reads a script is most likely to contain, and all of
    them are risk `ask` -- the fail-closed default for anything the catalog has
    not classified -- so all of them prompted, and piped, every prompt was a
    silent denial.
    """
    _piped_console(monkeypatch, "")
    pm.set_mode(pm.MANUAL)

    for command in ("/stats", "/whoami", "/sessions", "/todo"):
        assert sonder_repl._named_command_gate(command) == (True, ""), command


def test_a_piped_console_is_not_an_open_door(monkeypatch):
    """Degrading `ask` is not the same as degrading `deny`.

    The fix must be "nobody is here to answer", not "skip the gate when
    piped": an explicit deny rule and `plan` still refuse, exactly as they do
    for every other non-interactive caller.
    """
    _piped_console(monkeypatch, "")

    pm.set_mode(pm.MANUAL)
    may_run, refusal = sonder_repl._named_command_gate("/delete")
    assert not may_run
    assert "rule denies this tool" in refusal

    pm.set_mode(pm.PLAN)
    may_run, refusal = sonder_repl._named_command_gate("/write")
    assert not may_run
    assert refusal.startswith("refused /write:")


def test_the_catalogued_fallback_is_piped_the_same_way(monkeypatch):
    """`/<tool_name>` degrades identically -- both console entries share a seam."""
    piped = _piped_console(monkeypatch, "/exit\n")
    monkeypatch.setattr(server, "file_write", lambda **_kwargs: "wrote it")
    pm.set_mode(pm.MANUAL)

    output = sonder_repl._run_catalogued("/file_write path=x content=y", "/file_write")

    assert output == "wrote it"
    assert piped.readline() == "/exit\n"


def test_confirm_never_reads_a_line_nobody_typed(monkeypatch):
    """The safety net under the seam above.

    `_confirm` already treated EOF as "no", which covers the *last* line of a
    script. It did not cover the lines before it: those return a real string,
    so the read succeeds and the command vanishes. A function whose whole job
    is asking a person must not read at all when there is no person.
    """
    piped = _piped_console(monkeypatch, "y\n/exit\n")
    monkeypatch.setattr(
        builtins, "input",
        lambda *_a, **_k: pytest.fail("a piped session has nobody to prompt"),
    )

    assert sonder_repl._confirm("run it?") is False
    assert piped.readline() == "y\n", "the prompt consumed a line anyway"


def test_a_real_terminal_does_not_prompt_for_a_safe_observation(monkeypatch):
    """A verified safe observation bypasses the prompt even on a tty."""
    asked = []
    monkeypatch.setattr(
        sonder_repl, "_confirm", lambda question: asked.append(question) or False,
    )
    pm.set_mode(pm.MANUAL)

    may_run, refusal = sonder_repl._named_command_gate("/stats")

    assert (may_run, refusal) == (True, "")
    assert not asked


def test_an_unrecognised_risk_class_does_not_crash_the_console_gate(monkeypatch):
    """A new risk class must round to "most severe", not to a traceback.

    `_gate_tools` ranks the members of a multi-tool branch to decide which one
    to ask about. Ranking with a bare `_RISK_ORDER.index` made an unknown
    class raise `ValueError` from inside the gate, which at the console means
    the REPL loop dies on a typed command rather than refusing it.
    """
    monkeypatch.setattr(pm, "risk_of", lambda _tool: "newly-invented")
    asked = []
    monkeypatch.setattr(
        sonder_repl, "_confirm", lambda question: asked.append(question) or False,
    )
    pm.set_mode(pm.MANUAL)

    may_run, refusal = sonder_repl._gate_tools(("file_write", "file_edit"), "/probe")

    assert not may_run
    assert refusal == "skipped /probe"
    assert len(asked) == 1


def test_every_risky_console_tool_is_covered_by_the_derived_map():
    """No mutating/executing console tool may be missing from the gate's map.

    Attribution to the right command is what `_branch_tool_calls` does; this
    re-derives the far simpler question -- "does this tool get called from a
    console chain at all?" -- and demands the map account for every hit. A
    branch-boundary bug that dropped `/delete`'s body on the floor would leave
    `file_delete` uncovered here and fail, which is the point: coverage stops
    depending on anyone remembering to update a table.
    """
    covered = {
        tool for tools in command_catalog.console_tools().values() for tool in tools
    }
    called = set()
    for path, function in (("server.py", "control_command"), ("sonder_repl.py", "main")):
        called |= _tools_called_anywhere_in(path, function)

    # Everything that is not a verified read, `ask` included. `ask` is the
    # fail-closed default every unclassified tool lands in, and the class
    # `plan` denies -- so excluding it scoped this coverage test away from
    # precisely the bucket a newly added, unclassified tool arrives in. It
    # passes today with the exclusion removed: the `ask`-class tools reachable
    # from the console chains are already covered, so closing the bucket costs
    # nothing and turns a future silent gap into a failing test.
    risky = {tool for tool in called if pm.risk_of(tool) != "safe"}
    assert risky, "the walk found no risky console tool at all -- it is broken"
    assert risky - covered == set()


# --- the choke point's wiring, not just the function it calls -------------


def test_the_console_gate_runs_before_the_first_hand_written_branch():
    """The gate exists; this is the test that it is actually *called*.

    Twelve tests above drive `_named_command_gate` directly. None drove
    `main`, so deleting the one line that calls it left all of them green
    while the console was ungated again. The wiring is the part that cannot be
    checked by calling the gate yourself.

    Read out of the source in `_branch_tool_calls`' own idiom rather than by
    running the REPL loop: `main` is an unbounded read-eval loop around a
    ~500-line branch chain, and what needs pinning here is an ordering, not a
    behaviour.
    """
    with open(sonder_repl.__file__, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    scope = next(
        (node for node in ast.walk(tree)
         if isinstance(node, ast.FunctionDef) and node.name == "main"),
        None,
    )
    assert scope is not None, "sonder_repl.main no longer exists"

    gate_calls = [
        node.lineno for node in ast.walk(scope)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "_named_command_gate"
    ]
    branch_tests = [
        node.lineno for node in ast.walk(scope)
        if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name)
        and node.left.id == "cmd"
    ]

    assert gate_calls, "main no longer calls _named_command_gate at all"
    assert branch_tests, "the walk found no dispatch branch -- it is broken"
    assert min(gate_calls) < min(branch_tests), (
        "the gate runs after a dispatch branch, so that branch is ungated"
    )


# --- a branch whose real work no registered tool fronts -------------------


def test_selfmod_is_not_graded_by_its_most_harmless_sibling():
    """`/selfmod deploy` os.replace()s Sonder's own source tree.

    The only *registered tool* that branch calls is
    `system_improvement_report`, a `safe` read it quotes as evidence; the
    writer (`selfmod.deploy`) is a plain module function, so the
    registered-tools filter drops it. `_gate_tools` then took the strictest
    member of a set whose only member was `safe`, and the gate -- consulted --
    answered "allowed" under `plan`.

    That is worse than an uncovered path: a confident wrong answer from the
    mechanism built to give the right one.
    """
    assert pm.risk_of("selfmod") == "dangerous"
    for name in ("/selfmod", "/selfmodify"):
        mapped = command_catalog.console_tools().get(name, ())
        assert "selfmod" in mapped, name
        worst = min(
            (sonder_repl._severity(pm.risk_of(tool)) for tool in mapped),
            default=None,
        )
        assert worst == sonder_repl._severity("dangerous"), name


def test_plan_refuses_selfmod_at_the_console(monkeypatch):
    """The named requirement: `plan` must refuse the command that rewrites Sonder."""
    monkeypatch.setattr(
        sonder_repl, "_confirm",
        lambda _question: pytest.fail("a plan-mode denial must not prompt"),
    )
    pm.set_mode(pm.PLAN)

    for name in ("/selfmod", "/selfmodify"):
        may_run, refusal = sonder_repl._named_command_gate(name)
        assert not may_run, name
        assert refusal.startswith("refused %s:" % name), name


def test_auto_still_asks_before_selfmod(monkeypatch):
    """And it is `dangerous`, so even `auto` stops -- with an operator present."""
    asked = []
    monkeypatch.setattr(
        sonder_repl, "_confirm", lambda question: asked.append(question) or False,
    )
    pm.set_mode(pm.AUTO)

    may_run, refusal = sonder_repl._named_command_gate("/selfmod")

    assert (may_run, refusal) == (False, "skipped /selfmod")
    assert len(asked) == 1
    assert "dangerous" in asked[0]


# --- a catalog that cannot read the registry must refuse, not wave through --


@pytest.fixture
def broken_registry(monkeypatch):
    """A tool registry that raises, with the memoised catalog dropped after."""
    class _Broken:
        def list_tools(self):
            raise RuntimeError("registry not initialised")

    command_catalog.reset_cache()
    monkeypatch.setattr(server.mcp, "_tool_manager", _Broken())
    try:
        yield
    finally:
        monkeypatch.undo()
        command_catalog.reset_cache()


def test_a_catalog_that_cannot_read_the_registry_says_so(broken_registry):
    """It swallowed the failure and continued with an empty set.

    Two consequences, and the second is the worse one: every catalog-
    `dangerous` tool downgraded to `ask`, and `console_tools()` returned `{}`,
    which made `_gate_tools(())` answer `(True, "")` for every mapped console
    command. The gate did not break -- it turned off.
    """
    with pytest.raises(command_catalog.CatalogUnavailable):
        command_catalog.console_tools()


def test_the_console_gate_refuses_when_it_cannot_see_the_catalog(broken_registry):
    """Fail closed on ignorance: a gate that cannot tell must not allow."""
    pm.set_mode(pm.MANUAL)

    may_run, refusal = sonder_repl._named_command_gate("/delete")

    assert not may_run
    assert refusal.startswith("refused /delete:")
    assert "registry could not be read" in refusal


def test_a_dangerous_tool_does_not_downgrade_when_the_catalog_is_blind(broken_registry):
    """`risk_of` reads `dangerous` out of the catalog.

    A blind catalog silently reclassified `git_merge`, `sqlite_mutate`,
    `task_delete` and the rest as `ask`, and `auto` allows `ask` outright --
    so the mode that is supposed to stop for destructive tools stopped for
    nothing, with an operator sitting right there. Only `file_delete` and
    `admin_private_chain_of_thought` were saved, by shipped deny rules.

    This used to end by disclaiming the half that mattered: "a caller with
    nobody to ask still gets `allow` here ... that degrade is the 'preserve
    current behaviour' contract and is unchanged". Every production gate is
    such a caller, so the disclaimer covered the whole enforced surface -- the
    test proved the downgrade was fixed for the one caller never at risk, and
    recorded it as still broken for all the ones that were.

    Returning `dangerous` could not have fixed it either: `dangerous` is `ask`
    in manual, acceptEdits and auto, and the degrade allows it. A blind
    catalog now grades `unclassified`, which is not degraded, so the last
    three lines below are the property this test's title always claimed.
    """
    lookup = lambda _tool: None
    for tool in ("git_merge", "sqlite_mutate", "task_delete", "permission_rule_set"):
        assert pm.risk_of(tool) == pm.UNCLASSIFIED, tool
        assert pm.decide(
            tool, interactive=True, mode=pm.AUTO, rule_lookup=lookup,
        ).action == pm.ASK, tool
        assert pm.decide(
            tool, interactive=False, mode=pm.PLAN, rule_lookup=lookup,
        ).action == pm.DENY, tool
        # The half the old docstring waived. These are the modes an operator
        # actually runs in, reached by the callers that actually enforce.
        for mode in (pm.MANUAL, pm.ACCEPT_EDITS, pm.AUTO):
            assert pm.decide(
                tool, interactive=False, mode=mode, rule_lookup=lookup,
            ).action == pm.DENY, (tool, mode)


def test_a_transient_catalog_failure_does_not_latch(monkeypatch):
    """Both catalog results are `lru_cache(maxsize=1)`.

    A memoised degraded answer turns one transient failure -- a partially
    initialised server, exactly the case the code cited -- into enforcement
    that stays off for the life of the process, with nothing to notice it. An
    exception is not memoised, which is what makes re-raising the fix.
    """
    real = server.mcp._tool_manager
    calls = {"n": 0}

    class _BrokenOnce:
        def list_tools(self):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("registry not initialised")
            return real.list_tools()

    command_catalog.reset_cache()
    monkeypatch.setattr(server.mcp, "_tool_manager", _BrokenOnce())
    try:
        with pytest.raises(command_catalog.CatalogUnavailable):
            command_catalog.console_tools()

        # Same process, nothing reset: the next read must see the registry.
        assert "file_delete" in command_catalog.console_tools().get("/delete", ())
    finally:
        monkeypatch.undo()
        command_catalog.reset_cache()


# --- control_command has the same fall-through, on the chat path ----------


def test_the_control_command_fall_through_is_gated_by_tool_name(monkeypatch):
    """`server.control_command` ends in the same catalogued dispatch.

    It is reached from `answer_with_history` (`server.py:4055, :4237, :4292`)
    with the user's raw prompt, so a chat line reading `/file_delete path=x`
    ran the tool with no gate in front of it. Its branch chain is covered by
    the console map, but the fall-through *after* the chain is not -- the same
    shape as the app surface's, one file over.

    `interactive=False`: nothing here has a console attached. The console's own
    prompt already happened at `_named_command_gate` before it forwarded, and
    the chat path has nobody to ask -- so `ask` degrades to allow and only a
    `deny` rule and `plan` refuse.
    """
    monkeypatch.setattr(server, "file_delete", _never_runs)
    pm.set_mode(pm.PLAN)

    out = server.control_command("/file_delete path=notes.txt dry_run=false")

    assert out is not None
    assert out.startswith("refused /file_delete:")


def test_a_deny_rule_binds_on_the_control_command_fall_through(monkeypatch):
    monkeypatch.setattr(server, "file_delete", _never_runs)
    monkeypatch.setattr(pm, "_rule_lookup", _rules("deny"))
    pm.set_mode(pm.AUTO)

    out = server.control_command("/file_delete path=notes.txt")

    assert out.startswith("refused /file_delete:")
    assert "rule denies this tool" in out


def test_the_control_command_fall_through_still_runs_a_read(monkeypatch):
    """The over-correction guard: `/context_health` must answer in every mode."""
    monkeypatch.setattr(server, "context_health", lambda **_k: "CONTEXT OK")

    for mode in pm.MODES:
        pm.set_mode(mode)
        assert server.control_command("/context_health") == "CONTEXT OK", mode


def test_ordinary_prose_beginning_with_a_slash_still_reaches_the_model():
    """`control_command` returns None for a line that names nothing.

    Returning a refusal there would swallow the turn instead of answering it.
    """
    assert server.control_command("/not-a-tool-at-all what do you think") is None
    assert server.control_command("just a sentence") is None


# --- a blind catalog must not raise into the ordinary chat path -----------


def test_a_blind_catalog_does_not_raise_out_of_control_command(broken_registry):
    """`control_command` caught only `ValueError`, and `CatalogUnavailable` is
    a `RuntimeError`.

    It is reached unguarded from `answer_with_history`, so raising here turned
    a broken registry into an exception on the ordinary chat path, where it
    previously degraded. Fail closed *and* stay in-band: refuse, in a string.
    """
    out = server.control_command("/file_delete path=notes.txt")

    assert isinstance(out, str)
    assert out.startswith("refused")
    assert "registry could not be read" in out


def test_the_help_fallback_cannot_mask_a_type_error(broken_registry):
    """`help_command` inside `except TypeError` is a second unrouted read.

    Both `server.control_command` and `sonder_repl._run_catalogued` build
    their "you called it wrong" message by asking the catalog for usage. With
    a blind catalog that read raises, replacing the caller's real mistake with
    an unrelated `RuntimeError`.
    """
    assert command_catalog.help_command("/file_read").startswith(
        "the command catalog is unavailable"
    )


# --- the C2 floor must not depend on the incidental call that hid it ------


def test_the_selfmod_floor_survives_the_branch_losing_its_registered_call(monkeypatch):
    """`/selfmod`'s stand-in must not be conditional on the thing it fixes.

    The map only listed `/selfmod` at all because the branch happens to call
    `system_improvement_report`. If that incidental call ever goes away, the
    derivation produces no key -- and a floor that only augments existing keys
    would vanish with it, silently restoring the exact defect it was added
    for.
    """
    monkeypatch.setattr(
        command_catalog, "_branch_tool_calls", lambda *_a, **_k: {},
    )
    command_catalog.reset_cache()
    try:
        mapped = command_catalog.console_tools()
        assert mapped.get("/selfmod") == ("selfmod",)
        assert pm.risk_of("selfmod") == "dangerous"
    finally:
        monkeypatch.undo()
        command_catalog.reset_cache()


# --- the other two branches whose work no registered tool fronts ----------


def test_the_registry_refreshing_and_goal_writing_branches_are_graded():
    """Two more instances of C2's shape, found by the same reading.

    `/mcp refresh` republishes the live tool registry -- it changes what every
    later call is allowed to do, which is the reasoning `_DANGEROUS` already
    applies to `runtime_policy_update` and `permission_rule_set`. `/goal`
    writes to `goal_store` (adopt, complete, abandon, note). Neither branch
    calls a registered tool, so both mapped to nothing and were ungated.

    `/report` was read the same way and deliberately left alone: it only
    formats `activity_tracker.latest()`, so there is nothing to gate.
    """
    mapped = command_catalog.console_tools()
    assert mapped.get("/mcp") == ("mcp",)
    assert mapped.get("/convergence") == ("mcp",)
    assert mapped.get("/goal") == ("goal",)
    assert mapped.get("/goals") == ("goal",)
    assert "/report" not in mapped

    assert pm.risk_of("mcp") == "dangerous"
    assert pm.risk_of("goal") == "mutation"


def test_plan_refuses_a_registry_refresh_and_a_goal_write(monkeypatch):
    monkeypatch.setattr(
        sonder_repl, "_confirm",
        lambda _question: pytest.fail("a plan-mode denial must not prompt"),
    )
    pm.set_mode(pm.PLAN)

    for name in ("/mcp", "/convergence", "/goal", "/goals"):
        may_run, refusal = sonder_repl._named_command_gate(name)
        assert not may_run, name
        assert refusal.startswith("refused %s:" % name), name


def test_acceptedits_lets_a_goal_write_through_but_still_stops_a_registry_refresh(
    monkeypatch,
):
    """The two are graded differently, and the difference is the point.

    A goal note is an ordinary write, which `acceptEdits` exists to let flow.
    Republishing the tool registry is not, and no mode lets it pass unasked.
    """
    asked = []
    monkeypatch.setattr(
        sonder_repl, "_confirm", lambda question: asked.append(question) or False,
    )
    pm.set_mode(pm.ACCEPT_EDITS)

    assert sonder_repl._named_command_gate("/goal") == (True, "")
    assert not asked

    may_run, _refusal = sonder_repl._named_command_gate("/mcp")
    assert not may_run
    assert len(asked) == 1



# --- the whole control_command chain, from the path that does not gate ----


def test_the_control_command_chain_is_gated_from_the_chat_path(monkeypatch):
    """Three callers, and only two of them gated before forwarding.

    `sonder_repl` and `sonder_serve` consult their map and refuse first.
    `answer_with_history` -- the ordinary chat path, and what an MCP client
    reaches through the `sonder` tool -- passes the user's raw prompt straight
    in, so all 97 branches of this chain were reachable ungated from it. The
    fall-through at the end was gated last round; the chain above it was not.
    """
    monkeypatch.setattr(server, "directory_create", _never_runs)
    pm.set_mode(pm.PLAN)

    out = server.control_command("/mkdir newdir")

    assert out is not None
    assert out.startswith("refused /mkdir:")


def test_a_deny_rule_binds_on_the_control_command_chain(monkeypatch):
    monkeypatch.setattr(server, "file_delete", _never_runs)
    monkeypatch.setattr(pm, "_rule_lookup", _rules("deny"))
    pm.set_mode(pm.AUTO)

    out = server.control_command("/delete notes.txt")

    assert out.startswith("refused /delete:")
    assert "rule denies this tool" in out


def test_the_control_command_chain_still_answers_a_read(monkeypatch):
    """The over-correction guard: reads must keep working in every mode."""
    monkeypatch.setattr(server, "context_health", lambda **_k: "CONTEXT OK")

    for mode in pm.MODES:
        pm.set_mode(mode)
        assert server.control_command("/context") == "CONTEXT OK", mode


def test_gating_the_chain_cannot_double_prompt(monkeypatch):
    """The console gates, then forwards here. Nothing here may ask again.

    `interactive=False` is what makes re-deciding free: an `ask` the console
    already resolved degrades to allow on arrival rather than being put to the
    operator a second time.
    """
    import builtins

    monkeypatch.setattr(
        builtins, "input",
        lambda *_a, **_k: pytest.fail("control_command prompted"),
    )
    monkeypatch.setattr(server, "directory_create", lambda **_k: "made it")
    pm.set_mode(pm.MANUAL)

    assert server.control_command("/mkdir newdir") == "made it"


# --- the exemption has one owner, and it drifted the moment it had five ---


def test_the_gate_control_exemption_holds_at_every_person_facing_surface():
    """`permission_mode` must stay reachable wherever a person selects a mode.

    It is risk `ask`, which `plan` denies, and the mode persists to disk -- so
    a surface that gates it leaves whoever chose `plan` unable to choose
    anything else, across restarts. Four surfaces each kept their own copy of
    the check; the fifth, added one round ago, was written without it, and
    `decide("permission_mode", interactive=False, mode="plan")` came back
    `deny` on that path.
    """
    pm.set_mode(pm.PLAN)

    assert sonder_repl._named_command_gate("/mode") == (True, "")
    assert sonder_serve._http_tool_refusal(("permission_mode",), "/x") == ""
    assert server._control_tool_refusal(("permission_mode",), "/x") == ""
    assert reloadable_mcp._refuse_if_gated("permission_mode") is None


def test_the_agent_path_still_gets_no_exemption():
    """The distinction the exemption exists to draw, asserted in both halves.

    A person driving Sonder keeps a way out of `plan`. Sonder driving itself
    does not get to lift its own restraint.
    """
    pm.set_mode(pm.PLAN)

    assert pm.decide_for_caller(
        "permission_mode", interactive=False, gate_control_exempt=True,
    ) is None
    modelled = pm.decide_for_caller(
        "permission_mode", interactive=False, gate_control_exempt=False,
    )
    assert modelled is not None
    assert modelled.action == pm.DENY


def test_the_exemption_set_is_consulted_in_exactly_one_place():
    """What stops a sixth surface from being written without it.

    A set that has to be remembered at every call site is a set that will be
    missed at the next one -- which is exactly what happened when a fifth
    surface was added. Membership is now tested inside `decide_for_caller` and
    nowhere else, and this is the check that keeps it that way.

    Read as *code* rather than as text: an earlier version of this test
    grepped the source and flagged a docstring that merely mentions the set by
    name. Prose about a security-relevant set is not a use of it, and a test
    that cannot tell the difference trains people to reword comments.
    """
    import pathlib

    root = pathlib.Path(server.__file__).parent
    uses = {}
    for name in ("server.py", "sonder_repl.py", "sonder_serve.py",
                 "reloadable_mcp.py", "permission_modes.py"):
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            loaded = (
                isinstance(node, ast.Name)
                and node.id in ("GATE_CONTROL_TOOLS", "GATE_EXEMPT_TOOLS")
            ) or (
                isinstance(node, ast.Attribute)
                and node.attr in ("GATE_CONTROL_TOOLS", "GATE_EXEMPT_TOOLS")
            )
            if loaded and isinstance(getattr(node, "ctx", None), ast.Load):
                uses.setdefault(name, []).append(node.lineno)

    assert uses == {
        # The one membership test, inside decide_for_caller.
        "permission_modes.py": [permission_modes_check_line(root)],
        # The console's public alias, which exists only so a test can pin the
        # set to one name; it is not consulted at the gate any more.
        "sonder_repl.py": [alias_line(root)],
    }, "the exemption is consulted outside decide_for_caller: %r" % uses


def permission_modes_check_line(root):
    """The single line inside `decide_for_caller` that tests membership."""
    import pathlib

    source = (root / "permission_modes.py").read_text(encoding="utf-8").splitlines()
    hits = [
        number for number, line in enumerate(source, 1)
        if "in GATE_CONTROL_TOOLS" in line and not line.strip().startswith("#")
    ]
    assert len(hits) == 1, "expected exactly one membership test, found %r" % hits
    return hits[0]


def alias_line(root):
    source = (root / "sonder_repl.py").read_text(encoding="utf-8").splitlines()
    hits = [
        number for number, line in enumerate(source, 1)
        if line.startswith("GATE_EXEMPT_TOOLS = ")
    ]
    assert len(hits) == 1
    return hits[0]


# --- EXECUTION_COMMANDS cannot drift away from the map --------------------


def test_execution_commands_and_the_map_stay_in_step():
    """A stand-in graded `execution` must be a stand-in the map actually uses.

    Both directions: a name here that no branch maps to would be dead weight
    someone would later delete as unused, and a `run_code_window`-shaped
    branch added to the map without a grade here would silently be `ask`.
    """
    stand_ins = set(command_catalog._UNREGISTERED_BRANCH_WORK.values())

    assert pm.EXECUTION_COMMANDS <= stand_ins, (
        "EXECUTION_COMMANDS names something the map never produces"
    )
    for name in pm.EXECUTION_COMMANDS:
        assert pm.risk_of(name) == "execution", name
    assert not (pm.EXECUTION_COMMANDS & set(
        tool.name for tool in server.mcp._tool_manager.list_tools()
    )), "a registered tool belongs in EXECUTION_TOOLS, not here"
