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

import builtins

import pytest

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


def test_a_direct_mcp_tool_call_is_untouched_by_the_gate():
    """Direct MCP callers keep their old behaviour: the gate is not on that path.

    Only the two dispatch sites opt in, so the same tool that ``plan`` refuses
    through ``_agent_dispatch`` still runs when an MCP client calls it
    directly -- deliberately, because there is nobody there to prompt and this
    change is not allowed to break clients that have always worked.
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
