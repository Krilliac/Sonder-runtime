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
import os

import pytest
from mcp.server.fastmcp.exceptions import ToolError

import command_catalog
import permission_modes as pm
import permission_rules
import server
import sonder_repl
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

    Wiring the gate in flips two dormant layers on at once. The mode layer is
    a no-op on this path by construction (``interactive=False`` degrades
    ``ask`` to ``allow``), so the ONLY refusals mode ``manual`` may produce
    are the per-tool ``deny`` rules that ``/permissions`` has always printed
    and never enforced. Anything else refused here is a regression that would
    surface to a user as "Sonder stopped working".
    """
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

    assert refused == denied_by_rule
    # And the default policy really does deny something, so an empty set on
    # both sides can never pass this vacuously.
    assert "file_delete" in denied_by_rule


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

    risky = {tool for tool in called if pm.risk_of(tool) not in ("safe", "ask")}
    assert risky, "the walk found no risky console tool at all -- it is broken"
    assert risky - covered == set()
