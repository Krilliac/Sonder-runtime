"""Plan mode's read-only surface: what it must permit, and what it must not (#19).

`plan` advertises "reads only - no writes, no commands". Two separate defects
sat behind that sentence, and they point in opposite directions:

* **Too narrow.** ``command_catalog._risk_for`` calls a tool ``safe`` only when
  it appears in ``REPOSITORY_READ_ONLY_TOOLS`` or ``_WORK_INSPECTION_TOOLS``.
  Both sets are about *repository* work. Sixteen tools that only observe the
  running runtime -- ``task_list``, ``sonder_stats``, ``calibration_status``,
  ``mcp_runtime_status`` and friends -- are in neither, so they fell through to
  ``ask`` and ``plan`` denied every one of them.

* **Too wide, and worse.** Three tools that take a caller-supplied ``root`` --
  ``diff_files``, ``find_references``, ``secret_scan`` --
  are in ``_WORK_INSPECTION_TOOLS`` and were therefore ``safe``, so ``plan``
  allowed them. ``harness_tools._resolve_root`` performs no allowed-roots
  check, so under a mode whose whole promise is "reads only" a model could
  point ``secret_scan`` at any directory on the machine. ``b8a15ef`` removed
  exactly these four from ``REPOSITORY_READ_ONLY_TOOLS`` for this reason; the
  removal never reached the risk classification that ``plan`` consults.

Every name in ``_PLAN_PERMITTED`` below was verified by execution, in a fresh
interpreter each, against traps on file writes, process spawns, outbound
sockets and non-DDL SQL. Nothing here was accepted on the strength of its name.
"""
from __future__ import annotations

import ast
import inspect
import re

import command_catalog
import grounded_outcomes
import harness_tools
import permission_modes
import server


# Verified read-only by execution: zero file writes, zero subprocesses, zero
# outbound sockets, zero INSERT/UPDATE/DELETE, each on its SUCCESS path (a
# refusal or not-found path proves nothing) and each in a cold interpreter (a
# warm cache produced a false clean for npu_status).
_PLAN_PERMITTED = frozenset({
    "task_list", "task_show", "checklist_show",
    "admin_status", "admin_whoami",
    "autopilot_status", "calibration_status",
    "learn_tiers", "live_reload_status", "mcp_runtime_status",
    "reasoning_show", "sonder_sessions", "sonder_stats",
    "turn_inspect", "workflow_list", "memory_export",
})

# Refused, each for a reason observed by running it.
_PLAN_REFUSED = {
    "debug_inspect": "spawns nvidia-smi and powershell, and calls the model endpoint",
    "npu_status": "spawns powershell Get-CimInstance",
    "apply_learned": "three outbound model calls and INSERT OR REPLACE INTO vectors",
    "runtime_policy_status": "outbound call to the local model endpoint",
    "admin_accounts": "returns 'login required'; the success path could not be exercised",
    "permission_mode": "with a mode argument it rewrites the saved mode and leaves plan",
    "artifact_verify": "a grounded_outcomes VERIFIER: a run can write an outcome row",
    "ground_artifact": "a grounded_outcomes VERIFIER: a run can write an outcome row",
    "session_export": "no session fixture; the success path could not be exercised",
    "record_outcome": "writes an outcome row",
    "sonder_remember_fact": "writes a fact row",
    "task_create": "writes a task row",
}

# Take a caller-supplied root outside the harness confinement boundary.
_UNCONFINED = frozenset({
    "diff_files", "find_references", "secret_scan",
})


def _plan(tool):
    return permission_modes.decide(tool, interactive=False, mode="plan").action


def _registered():
    return frozenset(server.mcp._tool_manager._tools)


# --------------------------------------------------------------------------
# Non-vacuity. Every assertion below is worthless if these are wrong.
# --------------------------------------------------------------------------

def test_extractors_cannot_go_vacuous():
    reg = _registered()
    assert len(reg) >= 150, "tool registry looks empty: %d" % len(reg)
    assert _PLAN_PERMITTED <= reg, sorted(_PLAN_PERMITTED - reg)
    assert _UNCONFINED <= reg, sorted(_UNCONFINED - reg)
    # plan must still refuse a great deal, or "plan allows X" means nothing.
    denied = [t for t in reg if _plan(t) == permission_modes.DENY]
    assert len(denied) >= 80, "plan denies only %d of %d tools" % (len(denied), len(reg))
    # and must still allow the repository read surface it always allowed.
    assert _plan("file_read") == permission_modes.ALLOW
    assert _plan("text_search") == permission_modes.ALLOW


def test_the_unconfined_set_is_derived_from_harness_tools_not_restated():
    """_UNCONFINED must match the code, or this file rots into a fiction."""
    tree = ast.parse(inspect.getsource(harness_tools))
    users = {
        node.name for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and "_resolve_root" in ast.dump(node)
    }
    assert len(users) >= 10, "resolve-root extractor went vacuous: %d" % len(users)
    reached = set()
    for tool in _registered():
        fn = getattr(server, tool, None)
        if fn is None:
            continue
        try:
            src = inspect.getsource(getattr(fn, "fn", fn))
        except (OSError, TypeError):
            continue
        if any(re.search(r"harness_tools\.%s\b" % u, src) for u in users):
            reached.add(tool)
    assert _UNCONFINED <= reached, sorted(_UNCONFINED - reached)
    # The harness is now the confinement boundary: changing it back to a bare
    # resolver would silently re-open every direct developer-workflow tool.
    assert "_require_authorized_root" in inspect.getsource(harness_tools._resolve_root)


# --------------------------------------------------------------------------
# Too narrow: the sixteen read-only observation tools.
# --------------------------------------------------------------------------

def test_plan_permits_every_verified_read_only_observation_tool():
    refused = sorted(t for t in _PLAN_PERMITTED if _plan(t) != permission_modes.ALLOW)
    assert not refused, (
        "plan refuses %d tools that were verified read-only by execution: %s"
        % (len(refused), refused)
    )


def test_those_tools_are_classified_safe_rather_than_special_cased_in_the_mode():
    """The fix belongs in the risk classification, not in the plan row."""
    wrong = sorted(t for t in _PLAN_PERMITTED if permission_modes.risk_of(t) != "safe")
    assert not wrong, wrong


def test_manual_mode_stops_asking_about_them_too():
    """`manual` asks before anything that is not a read. These are reads."""
    asked = sorted(
        t for t in _PLAN_PERMITTED
        if permission_modes.decide(t, interactive=True, mode="manual").action
        != permission_modes.ALLOW
    )
    assert not asked, asked


# --------------------------------------------------------------------------
# Too wide: the four unconfined root-takers.
# --------------------------------------------------------------------------

def test_plan_refuses_tools_whose_root_argument_is_not_confined():
    allowed = sorted(t for t in _UNCONFINED if _plan(t) == permission_modes.ALLOW)
    assert not allowed, (
        "plan allows %s, which take a caller-supplied root that "
        "harness_tools._resolve_root does not confine" % allowed
    )


def test_no_tool_plan_allows_reaches_the_unconfined_root_resolver():
    """The general form: direct developer tools share a guarded resolver."""
    assert "_require_authorized_root" in inspect.getsource(harness_tools._resolve_root)
    tree = ast.parse(inspect.getsource(harness_tools))
    users = {
        node.name for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and "_resolve_root" in ast.dump(node)
    }
    reached = []
    for tool in sorted(_registered()):
        if _plan(tool) != permission_modes.ALLOW:
            continue
        fn = getattr(server, tool, None)
        if fn is None:
            continue
        try:
            src = inspect.getsource(getattr(fn, "fn", fn))
        except (OSError, TypeError):
            continue
        if any(re.search(r"harness_tools\.%s\b" % u, src) for u in users):
            reached.append(tool)
    assert "test_discover" in reached, reached


# --------------------------------------------------------------------------
# The refusals, each pinned with the reason it was refused.
# --------------------------------------------------------------------------

def test_plan_still_refuses_everything_that_was_observed_to_write_or_execute():
    allowed = sorted(t for t in _PLAN_REFUSED if _plan(t) == permission_modes.ALLOW)
    assert not allowed, [
        "%s (%s)" % (t, _PLAN_REFUSED[t]) for t in allowed
    ]


def test_permission_mode_can_leave_plan_mode_so_plan_may_never_allow_it():
    """The one refusal that is load-bearing for the mode itself."""
    assert _plan("permission_mode") == permission_modes.DENY
    sig = inspect.signature(getattr(server.permission_mode, "fn", server.permission_mode))
    assert "mode" in sig.parameters, (
        "permission_mode no longer takes a mode argument; re-derive this refusal"
    )


def test_nothing_plan_permits_can_file_a_grounded_outcome():
    """A generator or verifier writes rows; neither belongs in a read-only mode."""
    writers = sorted(
        t for t in _PLAN_PERMITTED
        if t in grounded_outcomes.GENERATORS or t in grounded_outcomes.VERIFIERS
    )
    assert not writers, writers


def test_nothing_plan_permits_takes_a_filesystem_root_argument():
    rootish = {"root", "path", "cwd", "extra_roots", "paths", "url"}
    offenders = {}
    for tool in sorted(_PLAN_PERMITTED):
        fn = getattr(server, tool)
        params = set(inspect.signature(getattr(fn, "fn", fn)).parameters)
        hit = params & rootish
        if hit:
            offenders[tool] = sorted(hit)
    assert not offenders, offenders


def test_this_file_and_server_name_the_same_two_sets():
    """Pin the literals above to the production sets they describe.

    Everything else here asserts behaviour (does `plan` allow it), which is the
    right thing to assert but leaves the set *names* unmentioned by any test --
    so `scripts/select_regression_tests.py` reported them as uncovered
    identifiers, and a rename would never select this file.
    """
    assert server._RUNTIME_OBSERVATION_TOOLS == _PLAN_PERMITTED
    assert server._UNCONFINED_ROOT_TOOLS == _UNCONFINED
    assert not (server._RUNTIME_OBSERVATION_TOOLS & server._UNCONFINED_ROOT_TOOLS)
    # The observation set is genuinely additive: none of it was already safe by
    # some other route, or this fix would be a no-op dressed up as a change.
    covered = server.REPOSITORY_READ_ONLY_TOOLS | server._WORK_INSPECTION_TOOLS
    assert not (server._RUNTIME_OBSERVATION_TOOLS & covered)


def test_the_catalog_and_the_mode_agree_about_execution_tools():
    """A tool that runs a host program is never 'safe', whatever the catalog says."""
    for tool in sorted(permission_modes.EXECUTION_TOOLS & _registered()):
        risk = permission_modes.risk_of(tool)
        assert risk in ("execution", "dangerous"), (tool, risk)
        assert _plan(tool) == permission_modes.DENY, tool
