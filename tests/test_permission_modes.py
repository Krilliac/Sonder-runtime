"""Drift and invariant tests for ``permission_modes``.

``permission_rules`` shipped per-tool allow/ask/deny rules that nothing ever
consulted -- ``check()`` had no callers, so the policy was displayed by
``/permissions`` and enforced nowhere. ``permission_modes`` is the decision
point that was missing, and it keeps two axes apart on purpose: autonomy (the
mode) and privilege (``elevated``).

Everything asserted here is re-derived from the same sources the module derives
from -- the real command catalog and the server's own policy frozensets -- so a
new runner tool or a reclassified risk fails a test rather than silently
widening what ``auto`` will do without asking.

The security-relevant invariants are the point of this file:

* ``dangerous`` never resolves to ``allow`` on an interactive surface, in any
  mode, including ``auto``.
* no mode change ever touches ``elevated()`` -- "auto" must not imply "admin".
* ``plan`` denies everything that is not a read, even with nobody to prompt.
* elevation is never restored from disk.
"""
from __future__ import annotations

import json
import threading

import pytest

import permission_modes as pm

pytestmark = pytest.mark.unit


# Rank for monotonicity: more permissive == higher.
_RANK = {pm.DENY: 0, pm.ASK: 1, pm.ALLOW: 2}

_RISK_CLASSES = ("safe", "ask", "mutation", "execution", "dangerous")

# No exemptions. There was one here for ``self_heal_repair`` while
# ``risk_of`` consulted ``EXECUTION_TOOLS`` ahead of the catalog; the
# precedence is now dangerous > execution, so nothing is excused and a
# regression fails outright.


# --- isolation ------------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def _guard_process_state():
    """Fail this module if it leaks process-wide state into other suites.

    ``permission_modes`` holds module-level state and writes a JSON file under
    the Sonder home. A test file that left the mode on ``auto`` or elevation on
    would quietly change what every later test file decides.
    """
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
    assert pm._rule_lookup is before_rule_lookup, "_rule_lookup monkeypatch was not undone"


@pytest.fixture(autouse=True)
def state_file(tmp_path, monkeypatch):
    """Per-test sandbox: a tmp state file and a known-good starting state."""
    path = tmp_path / "permission_mode.json"
    saved = dict(pm._STATE)
    saved_loaded = pm._LOADED
    monkeypatch.setattr(pm, "_state_path", lambda: str(path))
    with pm._LOCK:
        pm._STATE.update(mode=pm.DEFAULT_MODE, elevated=False, elevation_reason="")
    # Pretend the load already happened so a test starts from the state above;
    # tests that exercise _load() explicitly reset the flag themselves.
    pm._LOADED = True
    try:
        yield path
    finally:
        with pm._LOCK:
            pm._STATE.update(saved)
        pm._LOADED = saved_loaded


@pytest.fixture(autouse=True)
def no_rule_by_default(monkeypatch):
    """Neutralize decide()'s rule hook so every other test stays hermetic.

    ``decide()``'s default rule source (``permission_modes._rule_lookup``,
    normally ``_default_rule_lookup``) reads a real, machine-specific
    ``permissions.json`` via ``sonder_paths.default_home()``. Leaving that
    wired in during tests would make every test in this file depend on
    whatever happens to be on the disk of whoever/whatever runs them --
    exactly what the rule-lookup injection point exists to avoid. This makes
    "no rule_lookup given" mean "no rule matched" here, i.e. decide() behaves
    exactly as it did before rules were wired in, unless a test explicitly
    monkeypatches ``pm._rule_lookup`` (or passes ``rule_lookup=``) again.
    """
    monkeypatch.setattr(pm, "_rule_lookup", lambda tool_name: None)


@pytest.fixture(scope="module")
def catalog_commands():
    import command_catalog

    commands = command_catalog.catalog()
    assert len(commands) > 100, "catalog collapsed; the drift tests below mean nothing"
    return commands


@pytest.fixture(scope="module")
def dangerous_tools(catalog_commands):
    names = sorted({c.tool for c in catalog_commands if c.risk == "dangerous" and c.tool})
    assert names, "no dangerous tools in the catalog -- the invariant is untestable"
    return names


@pytest.fixture(scope="module")
def registered_tools():
    import server

    names = {tool.name for tool in server.mcp._tool_manager.list_tools()}
    assert len(names) > 100, "tool manager returned almost nothing; drift check is a floor"
    return names


# --- invariant 1: dangerous never flows -----------------------------------


def test_matrix_never_allows_the_dangerous_class_in_any_mode():
    for mode in pm.MODES:
        assert pm._MATRIX[mode]["dangerous"] != pm.ALLOW, (
            "mode %r allows dangerous tools outright" % mode
        )


def test_every_mode_is_covered_by_the_matrix_for_every_risk_class():
    assert set(pm._MATRIX) == set(pm.MODES)
    for mode in pm.MODES:
        assert set(pm._MATRIX[mode]) == set(_RISK_CLASSES), (
            "mode %r does not cover every risk class" % mode
        )
        for risk, action in pm._MATRIX[mode].items():
            assert action in _RANK, "mode %r risk %r has bogus action %r" % (
                mode, risk, action,
            )


def test_no_catalog_dangerous_tool_is_ever_allowed(dangerous_tools):
    """The module's headline promise, asserted against the real catalog.

    This was a strict xfail: ``risk_of`` consulted ``EXECUTION_TOOLS`` before
    the catalog, so ``self_heal_repair`` -- in both sets -- reported as
    ``execution``, which ``auto`` allows outright. The precedence is now
    dangerous > execution, so this holds and is asserted for real.
    """
    for mode in pm.MODES:
        for tool in dangerous_tools:
            assert pm.decide(tool, mode=mode).action != pm.ALLOW, (
                "%s is allowed outright in %s" % (tool, mode)
            )


def test_dangerous_outranks_execution_for_a_tool_in_both_buckets(catalog_commands):
    """Membership of both sets is fine; being *classed* as the weaker one is not.

    Asserting the two sets never overlap was the wrong guard -- it forbade a
    harmless condition while the actual defect was one of precedence. What
    matters is that a tool in both resolves to ``dangerous``, so ``auto`` keeps
    asking about it.
    """
    dangerous = {c.tool for c in catalog_commands if c.risk == "dangerous" and c.tool}
    overlap = dangerous & set(pm.EXECUTION_TOOLS)
    assert overlap, (
        "expected at least one tool in both sets (self_heal_repair); without "
        "one this test proves nothing about precedence"
    )
    for tool in sorted(overlap):
        assert pm.risk_of(tool) == "dangerous", (
            "%s is in both buckets and resolved as %r -- execution is shadowing "
            "its dangerous classification" % (tool, pm.risk_of(tool))
        )


def test_dangerous_still_asks_in_auto_for_a_representative_tool():
    decision = pm.decide("file_delete", mode=pm.AUTO)
    assert decision.action == pm.ASK
    assert decision.risk == "dangerous"
    assert decision.allowed is False
    assert decision.to_dict()["tool"] == "file_delete"


# --- invariant 2: no mode change touches elevation ------------------------


@pytest.mark.parametrize("start_elevated", [False, True])
def test_no_mode_change_ever_alters_elevation(start_elevated):
    pm.set_elevated(start_elevated, "test reason" if start_elevated else "")
    reason_before = pm.elevation_reason()

    for mode in pm.MODES:
        pm.set_mode(mode)
        assert pm.elevated() is start_elevated, (
            "set_mode(%r) changed elevation" % mode
        )
        assert pm.elevation_reason() == reason_before

    for _ in range(len(pm.MODES) * 2 + 1):
        pm.cycle_mode()
        assert pm.elevated() is start_elevated, "cycle_mode() changed elevation"
    for _ in range(len(pm.MODES) * 2 + 1):
        pm.cycle_mode(-1)
        assert pm.elevated() is start_elevated, "cycle_mode(-1) changed elevation"

    assert pm.elevation_reason() == reason_before


def test_no_mode_grants_elevation_from_a_clean_start():
    assert pm.elevated() is False
    for mode in pm.MODES:
        pm.set_mode(mode)
        assert pm.elevated() is False
    pm.set_mode(pm.AUTO)
    assert pm.elevated() is False, "auto must not imply administrator"


def test_set_elevated_toggles_and_clears_its_reason():
    assert pm.set_elevated(True, "installing a driver") is True
    assert pm.elevated() is True
    assert pm.elevation_reason() == "installing a driver"
    assert pm.set_elevated(False) is False
    assert pm.elevated() is False
    assert pm.elevation_reason() == "", "reason must not survive de-elevation"


def test_privileged_tools_are_denied_without_elevation(monkeypatch):
    monkeypatch.setattr(pm, "PRIVILEGED_TOOLS", frozenset({"file_read"}))
    pm.set_mode(pm.AUTO)
    pm.set_elevated(False)
    denied = pm.decide("file_read")
    assert denied.action == pm.DENY
    assert "elevation" in denied.reason
    pm.set_elevated(True, "test")
    assert pm.decide("file_read").action == pm.ALLOW


def test_privileged_tools_is_empty_by_default():
    """Locks the decision made in the module docstring's rationale section.

    Not a drift guard -- a deliberate choice. If this ever fails because
    someone added a tool, that addition needs the same evidence-backed
    justification the docstring asks for, not a quiet edit to this test.
    """
    assert pm.PRIVILEGED_TOOLS == frozenset()


def test_requires_elevation_denies_without_elevation_and_allows_with_it():
    """The capability-gate path: privilege on ONE call, not a tool-wide list.

    file_read is not in PRIVILEGED_TOOLS and never will be for this test --
    the caller flags this specific invocation instead.
    """
    pm.set_mode(pm.AUTO)
    pm.set_elevated(False)
    denied = pm.decide("file_read", requires_elevation=True)
    assert denied.action == pm.DENY
    assert "elevation" in denied.reason
    pm.set_elevated(True, "test")
    assert pm.decide("file_read", requires_elevation=True).action == pm.ALLOW


def test_requires_elevation_defaults_to_false_and_changes_nothing():
    """Passing no requires_elevation must be byte-for-byte the old behaviour."""
    pm.set_elevated(False)
    for mode in pm.MODES:
        for tool in ("file_read", "file_write", "workspace_run", "file_delete"):
            with_default = pm.decide(tool, mode=mode)
            explicit_false = pm.decide(tool, mode=mode, requires_elevation=False)
            assert with_default == explicit_false


def test_requires_elevation_does_not_leak_onto_other_tools_or_calls():
    """One call's requires_elevation must not become a standing restriction."""
    pm.set_elevated(False)
    pm.decide("file_read", requires_elevation=True)  # denied, discarded
    assert pm.decide("file_read").action == pm.ALLOW, (
        "requires_elevation from a prior call leaked into a later one"
    )
    assert pm.decide("file_write", mode=pm.AUTO, requires_elevation=False).action == pm.ALLOW


def test_rule_deny_still_outranks_requires_elevation(monkeypatch):
    """Precedence order 1 (rule deny) is checked before 2 (privilege)."""
    monkeypatch.setattr(pm, "_rule_lookup", _rule(pm.DENY, pattern="file_read"))
    pm.set_elevated(True, "test")
    decision = pm.decide("file_read", requires_elevation=True)
    assert decision.action == pm.DENY
    assert "rule" in decision.reason.lower()


# --- invariant 3: plan holds still ----------------------------------------


@pytest.mark.parametrize("risk", [r for r in _RISK_CLASSES if r != "safe"])
def test_plan_denies_every_non_safe_risk_class(risk):
    assert pm._MATRIX[pm.PLAN][risk] == pm.DENY


def test_plan_allows_reads():
    assert pm._MATRIX[pm.PLAN]["safe"] == pm.ALLOW
    assert pm.decide("file_read", mode=pm.PLAN).action == pm.ALLOW


@pytest.mark.parametrize("tool", ["file_write", "workspace_run", "file_delete", "sonder"])
@pytest.mark.parametrize("interactive", [True, False])
def test_plan_denies_regardless_of_interactivity(tool, interactive):
    decision = pm.decide(tool, mode=pm.PLAN, interactive=interactive)
    assert decision.action == pm.DENY, (
        "plan let %s through with interactive=%s" % (tool, interactive)
    )
    assert decision.allowed is False


# --- invariant 4: elevation is not restored from disk ---------------------


def test_elevation_is_not_restored_from_disk(state_file):
    state_file.write_text(
        json.dumps({"mode": pm.AUTO, "elevated": True,
                    "elevation_reason": "from a previous session"}),
        encoding="utf-8",
    )
    with pm._LOCK:
        pm._STATE.update(mode=pm.DEFAULT_MODE, elevated=False, elevation_reason="")
    pm._LOADED = False

    assert pm.current_mode() == pm.AUTO, "the mode should be restored"
    assert pm.elevated() is False, "elevation must never be restored from disk"
    assert pm.elevation_reason() == ""


def test_saved_state_never_contains_elevation(state_file):
    pm.set_elevated(True, "session only")
    pm.set_mode(pm.AUTO)
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved == {"mode": pm.AUTO}
    assert "elevated" not in saved


def test_load_ignores_a_bogus_or_corrupt_saved_mode(state_file):
    for payload in ('{"mode": "godmode"}', "not json at all", "{}"):
        state_file.write_text(payload, encoding="utf-8")
        with pm._LOCK:
            pm._STATE["mode"] = pm.DEFAULT_MODE
        pm._LOADED = False
        assert pm.current_mode() == pm.DEFAULT_MODE, (
            "a corrupt state file changed the mode: %r" % payload
        )


def test_missing_state_file_falls_back_to_the_default_mode(state_file):
    assert not state_file.exists()
    with pm._LOCK:
        pm._STATE["mode"] = pm.DEFAULT_MODE
    pm._LOADED = False
    assert pm.current_mode() == pm.DEFAULT_MODE


def test_default_mode_is_the_conservative_one():
    assert pm.DEFAULT_MODE == pm.MANUAL
    assert pm._MATRIX[pm.DEFAULT_MODE]["mutation"] == pm.ASK


# --- matrix shape and monotonicity ----------------------------------------


def test_mode_order_is_least_to_most_autonomous():
    assert pm.MODES == (pm.PLAN, pm.MANUAL, pm.ACCEPT_EDITS, pm.AUTO)


@pytest.mark.parametrize("risk", _RISK_CLASSES)
def test_actions_never_get_more_restrictive_along_the_mode_order(risk):
    ranks = [_RANK[pm._MATRIX[mode][risk]] for mode in pm.MODES]
    assert ranks == sorted(ranks), (
        "risk %r is non-monotonic across %s: %s"
        % (risk, " -> ".join(pm.MODES),
           ", ".join("%s=%s" % (m, pm._MATRIX[m][risk]) for m in pm.MODES))
    )


def test_each_mode_differs_from_its_neighbour_by_something_real():
    """Four modes only earn their keep if each moves a boundary."""
    for earlier, later in zip(pm.MODES, pm.MODES[1:]):
        assert pm._MATRIX[earlier] != pm._MATRIX[later], (
            "%s and %s are the same policy" % (earlier, later)
        )


def test_accept_edits_and_auto_differ_on_execution_only():
    accept, auto = pm._MATRIX[pm.ACCEPT_EDITS], pm._MATRIX[pm.AUTO]
    differing = {k for k in accept if accept[k] != auto[k]}
    assert differing == {"execution"}, (
        "the acceptEdits/auto boundary is meant to be execution, not risk: %s"
        % sorted(differing)
    )
    assert accept["execution"] == pm.ASK
    assert auto["execution"] == pm.ALLOW


# --- decide() semantics ---------------------------------------------------


def test_ask_degrades_to_allow_when_not_interactive():
    decision = pm.decide("file_write", mode=pm.MANUAL, interactive=False)
    assert decision.action == pm.ALLOW
    assert "no interactive prompt available" in decision.reason
    assert pm.decide("file_write", mode=pm.MANUAL, interactive=True).action == pm.ASK


def test_non_interactive_degradation_does_not_apply_under_plan():
    assert pm.decide("file_write", mode=pm.PLAN, interactive=False).action == pm.DENY


def test_deny_is_never_softened_by_non_interactivity():
    for risk, action in pm._MATRIX[pm.PLAN].items():
        if action != pm.DENY:
            continue
        tool = next(
            (name for name in ("file_write", "workspace_run", "file_delete", "sonder")
             if pm.risk_of(name) == risk),
            None,
        )
        if tool is None:
            continue
        assert pm.decide(tool, mode=pm.PLAN, interactive=False).action == pm.DENY


def test_unknown_tool_is_never_treated_as_safe():
    """Fail closed: an unregistered name must not sail through as a read."""
    for name in ("not_a_real_tool", "rm_rf_slash", "file_read_but_not_really"):
        assert pm.risk_of(name) != "safe", "%s classified as safe" % name
        assert pm.risk_of(name) == "ask"
        assert pm.decide(name, mode=pm.MANUAL).action == pm.ASK
        assert pm.decide(name, mode=pm.PLAN).action == pm.DENY


def test_decide_uses_the_current_mode_when_none_is_given():
    pm.set_mode(pm.PLAN)
    assert pm.decide("file_write").action == pm.DENY
    assert pm.decide("file_write").mode == pm.PLAN
    pm.set_mode(pm.ACCEPT_EDITS)
    assert pm.decide("file_write").action == pm.ALLOW
    assert pm.decide("file_write").mode == pm.ACCEPT_EDITS


def test_decide_strips_a_leading_slash():
    assert pm.decide("/file_delete", mode=pm.AUTO).tool == "file_delete"
    assert pm.risk_of("/file_delete") == pm.risk_of("file_delete") == "dangerous"


def test_decision_reason_names_the_mode_and_the_risk():
    decision = pm.decide("file_write", mode=pm.MANUAL)
    assert pm.MODE_LABELS[pm.MANUAL] in decision.reason
    assert decision.risk in decision.reason
    assert decision.to_dict() == {
        "action": decision.action, "mode": decision.mode, "risk": decision.risk,
        "reason": decision.reason, "tool": decision.tool,
    }


def test_decision_allowed_property_tracks_the_action():
    assert pm.decide("file_read", mode=pm.AUTO).allowed is True
    assert pm.decide("file_write", mode=pm.MANUAL).allowed is False
    assert pm.decide("file_write", mode=pm.PLAN).allowed is False


def test_execution_tools_are_classified_execution_not_whatever_the_catalog_says():
    for tool in ("workspace_run", "script_run", "run_code", "build_run", "test_run"):
        assert pm.risk_of(tool) == "execution", tool
    assert pm.decide("workspace_run", mode=pm.ACCEPT_EDITS).action == pm.ASK
    assert pm.decide("workspace_run", mode=pm.AUTO).action == pm.ALLOW


# --- rules and modes compose, they do not race -----------------------------
#
# permission_rules shipped per-tool allow/ask/deny rules that decide() never
# consulted. These pin the precedence from the module docstring: an explicit
# deny always wins (even over auto); an explicit allow only ever loosens an
# ask (never touches an already-allowing mode, and never touches plan's
# deny); anything else is inert and the mode alone decides, unchanged.


def _rule(action, pattern="*", note="test rule"):
    return lambda tool_name: {"pattern": pattern, "action": action, "note": note}


def test_rule_deny_beats_every_mode_including_auto(monkeypatch):
    monkeypatch.setattr(pm, "_rule_lookup", _rule(pm.DENY, pattern="file_write"))
    for mode in pm.MODES:
        decision = pm.decide("file_write", mode=mode)
        assert decision.action == pm.DENY, "rule deny lost to mode %r" % mode
        assert "rule" in decision.reason.lower()
        assert "file_write" in decision.reason


def test_rule_deny_outranks_auto_for_an_execution_tool(monkeypatch):
    monkeypatch.setattr(pm, "_rule_lookup", _rule(pm.DENY, pattern="workspace_run"))
    assert pm._MATRIX[pm.AUTO]["execution"] == pm.ALLOW, "test assumption changed upstream"
    decision = pm.decide("workspace_run", mode=pm.AUTO)
    assert decision.action == pm.DENY
    assert decision.allowed is False


def test_rule_deny_is_immune_to_the_non_interactive_degrade(monkeypatch):
    monkeypatch.setattr(pm, "_rule_lookup", _rule(pm.DENY, pattern="file_write"))
    decision = pm.decide("file_write", mode=pm.MANUAL, interactive=False)
    assert decision.action == pm.DENY, (
        "a rule deny must not be softened just because nobody can be asked"
    )


def test_rule_allow_satisfies_manuals_ask(monkeypatch):
    monkeypatch.setattr(pm, "_rule_lookup", _rule(pm.ALLOW, pattern="workspace_run"))
    assert pm._MATRIX[pm.MANUAL]["execution"] == pm.ASK, "test assumption changed upstream"
    decision = pm.decide("workspace_run", mode=pm.MANUAL)
    assert decision.action == pm.ALLOW
    assert decision.allowed is True
    assert "rule" in decision.reason.lower()
    assert "manual" in decision.reason.lower()


def test_rule_allow_does_not_override_plans_deny(monkeypatch):
    monkeypatch.setattr(pm, "_rule_lookup", _rule(pm.ALLOW, pattern="file_write"))
    decision = pm.decide("file_write", mode=pm.PLAN)
    assert decision.action == pm.DENY, (
        "plan's entire purpose is holding still; a rule must not undo that"
    )
    assert decision.mode == pm.PLAN


def test_rule_allow_is_moot_and_unattributed_when_the_mode_already_allows(monkeypatch):
    monkeypatch.setattr(pm, "_rule_lookup", _rule(pm.ALLOW, pattern="file_write"))
    assert pm._MATRIX[pm.AUTO]["mutation"] == pm.ALLOW, "test assumption changed upstream"
    decision = pm.decide("file_write", mode=pm.AUTO)
    assert decision.action == pm.ALLOW
    # The mode decided this on its own; the reason must say so, not credit a
    # rule that had nothing to do with the outcome.
    assert "rule" not in decision.reason.lower()
    assert "auto allows" in decision.reason


def test_no_matching_rule_falls_through_to_mode_behaviour_unchanged():
    # The autouse no_rule_by_default fixture already makes "no rule_lookup
    # configured" mean "no rule matched"; this asserts that is indistinguishable
    # from passing an explicit lookup that finds nothing, across several modes
    # and risk classes.
    cases = [
        ("file_write", pm.MANUAL),
        ("workspace_run", pm.ACCEPT_EDITS),
        ("file_delete", pm.AUTO),
        ("status", pm.PLAN),
        ("file_read", pm.AUTO),
    ]
    for tool, mode in cases:
        implicit = pm.decide(tool, mode=mode)
        explicit = pm.decide(tool, mode=mode, rule_lookup=lambda name: None)
        assert implicit == explicit, (tool, mode)


def test_rule_action_of_ask_is_inert_not_a_third_kind_of_restriction(monkeypatch):
    monkeypatch.setattr(pm, "_rule_lookup", _rule(pm.ASK, pattern="workspace_run"))
    # auto allows execution outright; an explicit "ask" rule must not
    # retroactively invent a new, stricter behaviour the mode never had.
    decision = pm.decide("workspace_run", mode=pm.AUTO)
    assert decision.action == pm.ALLOW
    assert "rule" not in decision.reason.lower()


def test_rule_with_unrecognised_action_is_inert(monkeypatch):
    monkeypatch.setattr(pm, "_rule_lookup", _rule("wat", pattern="file_write"))
    decision = pm.decide("file_write", mode=pm.MANUAL)
    assert decision.action == pm.ASK
    assert "rule" not in decision.reason.lower()


def test_rule_lookup_param_overrides_the_module_hook_for_one_call(monkeypatch):
    monkeypatch.setattr(pm, "_rule_lookup", _rule(pm.DENY, pattern="file_write"))
    decision = pm.decide("file_write", mode=pm.AUTO, rule_lookup=lambda name: None)
    assert decision.action == pm.ALLOW, "the per-call rule_lookup must win over the module hook"


def test_a_broken_rule_lookup_fails_closed_not_open(monkeypatch):
    def boom(tool_name):
        raise RuntimeError("disk exploded")

    monkeypatch.setattr(pm, "_rule_lookup", boom)
    decision = pm.decide("not_a_real_tool", mode=pm.MANUAL)
    assert decision.action == pm.ASK
    assert decision.risk == "ask"
    decision_plan = pm.decide("not_a_real_tool", mode=pm.PLAN)
    assert decision_plan.action == pm.DENY


def test_a_lookup_returning_garbage_is_treated_as_no_rule(monkeypatch):
    for garbage in (None, "deny", 42, ["deny"], {"pattern": "file_write"}):
        # A dict with no recognised action (or not a dict at all) must be
        # ignored, same as no rule matching. A dict WITH a recognised action
        # (e.g. {"action": "deny"}) is covered separately -- that one should
        # apply even with no pattern.
        monkeypatch.setattr(pm, "_rule_lookup", lambda name, g=garbage: g)
        decision = pm.decide("file_write", mode=pm.AUTO)
        assert decision.action == pm.ALLOW, "garbage rule %r was not ignored" % (garbage,)


def test_a_rule_with_a_recognised_action_but_no_pattern_still_applies(monkeypatch):
    monkeypatch.setattr(pm, "_rule_lookup", lambda name: {"action": "deny"})
    decision = pm.decide("file_write", mode=pm.AUTO)
    assert decision.action == pm.DENY


def test_privileged_check_outranks_a_rule_allow(monkeypatch):
    monkeypatch.setattr(pm, "PRIVILEGED_TOOLS", frozenset({"file_read"}))
    monkeypatch.setattr(pm, "_rule_lookup", _rule(pm.ALLOW, pattern="file_read"))
    pm.set_elevated(False)
    decision = pm.decide("file_read", mode=pm.AUTO)
    assert decision.action == pm.DENY, "a rule must not be able to grant elevation"
    assert "elevation" in decision.reason


def test_default_rule_lookup_reads_the_real_on_disk_policy(tmp_path, monkeypatch):
    import sonder_paths

    monkeypatch.setattr(sonder_paths, "default_home", lambda: tmp_path)

    # No permissions.json yet: permission_rules falls back to its built-in
    # defaults, which include an explicit file_delete deny.
    rule = pm._default_rule_lookup("file_delete")
    assert rule is not None
    assert rule["action"] == "deny"

    # A tool nothing matches must come back as None ("no rule"), not the
    # permissive NO_MATCH_RULE dict -- decide() only special-cases allow/deny,
    # but a caller inspecting the rule directly should see "nothing matched".
    assert pm._default_rule_lookup("totally_unregistered_tool_xyz") is None


def test_decide_wires_to_the_real_policy_file_end_to_end(tmp_path, monkeypatch):
    """Prove the production path, not just the injection point.

    Restores the real ``_default_rule_lookup`` (the no_rule_by_default fixture
    neutralizes it for every other test) and points ``sonder_paths.default_home``
    at a tmp policy file, so this exercises exactly what a real dispatch would.
    """
    import sonder_paths

    monkeypatch.setattr(sonder_paths, "default_home", lambda: tmp_path)
    monkeypatch.setattr(pm, "_rule_lookup", pm._default_rule_lookup)
    (tmp_path / "permissions.json").write_text(
        json.dumps([{"pattern": "workspace_run", "action": "allow",
                      "note": "trusted by the operator"}]),
        encoding="utf-8",
    )

    decision = pm.decide("workspace_run", mode=pm.MANUAL)
    assert decision.action == pm.ALLOW
    assert "rule" in decision.reason.lower()


# --- drift: EXECUTION_TOOLS vs the real surface ---------------------------


def test_every_execution_tool_is_a_real_registered_tool(registered_tools):
    stale = sorted(name for name in pm.EXECUTION_TOOLS if name not in registered_tools)
    assert not stale, (
        "permission_modes.EXECUTION_TOOLS names tools that no longer exist, so "
        "they silently classify nothing: %s" % ", ".join(stale)
    )


def test_server_project_scoped_execution_tools_are_all_covered():
    import server

    missing = sorted(server._PROJECT_SCOPED_EXECUTION_TOOLS - pm.EXECUTION_TOOLS)
    assert not missing, (
        "server treats these as execution but permission_modes does not, so "
        "acceptEdits would run them without asking: %s" % ", ".join(missing)
    )


def test_autopilot_runners_are_programs_not_tools():
    """Guard the two namespaces from being conflated by a future edit."""
    import server

    overlap = sorted(set(server._AUTOPILOT_RUNNERS) & set(pm.EXECUTION_TOOLS))
    assert not overlap, (
        "_AUTOPILOT_RUNNERS holds executable names, EXECUTION_TOOLS holds MCP "
        "tool names; these collided: %s" % ", ".join(overlap)
    )


def test_read_only_tools_are_not_marked_execution():
    import server

    overlap = sorted(server.REPOSITORY_READ_ONLY_TOOLS & pm.EXECUTION_TOOLS)
    assert not overlap, (
        "these are on the repository read-only allowlist yet classed as "
        "execution: %s" % ", ".join(overlap)
    )


def test_every_catalog_risk_class_is_handled_by_the_matrix(catalog_commands):
    seen = {c.risk for c in catalog_commands}
    unhandled = sorted(seen - set(_RISK_CLASSES))
    assert not unhandled, (
        "the catalog emits risk classes the mode matrix has no row for, so "
        "decide() falls back to ASK for them: %s" % ", ".join(unhandled)
    )


def test_every_registered_tool_gets_a_known_risk_class(registered_tools):
    bad = sorted(n for n in registered_tools if pm.risk_of(n) not in _RISK_CLASSES)
    assert not bad, "risk_of returned an unknown class for: %s" % ", ".join(bad)


# --- state handling: set_mode / cycle_mode --------------------------------


@pytest.mark.parametrize("mode", pm.MODES)
def test_set_mode_accepts_every_exact_name(mode):
    assert pm.set_mode(mode) == mode
    assert pm.current_mode() == mode


@pytest.mark.parametrize(
    "spelling,expected",
    [
        ("acceptEdits", pm.ACCEPT_EDITS),
        ("acceptedits", pm.ACCEPT_EDITS),
        ("ACCEPTEDITS", pm.ACCEPT_EDITS),
        ("accept edits", pm.ACCEPT_EDITS),
        ("accept-edits", pm.ACCEPT_EDITS),
        ("  Accept Edits  ", pm.ACCEPT_EDITS),
        ("PLAN", pm.PLAN),
        ("Manual", pm.MANUAL),
        ("AUTO", pm.AUTO),
    ],
)
def test_set_mode_is_case_space_and_hyphen_tolerant(spelling, expected):
    assert pm.set_mode(spelling) == expected
    assert pm.current_mode() == expected


@pytest.mark.parametrize(
    "prefix,expected",
    [("p", pm.PLAN), ("pl", pm.PLAN), ("m", pm.MANUAL), ("au", pm.AUTO),
     ("acc", pm.ACCEPT_EDITS), ("accept", pm.ACCEPT_EDITS)],
)
def test_set_mode_accepts_unambiguous_prefixes(prefix, expected):
    assert pm.set_mode(prefix) == expected


@pytest.mark.parametrize("bad", ["", "   ", None, "a", "godmode", "yolo", "planet",
                                 "accept_edits", "1"])
def test_set_mode_rejects_nonsense_with_value_error(bad):
    pm.set_mode(pm.MANUAL)
    with pytest.raises(ValueError):
        pm.set_mode(bad)
    assert pm.current_mode() == pm.MANUAL, "a rejected mode must not change state"


def test_set_mode_rejects_an_ambiguous_prefix():
    # "a" matches both acceptEdits and auto.
    with pytest.raises(ValueError):
        pm.set_mode("a")


def test_set_mode_persists_to_the_state_file(state_file):
    pm.set_mode(pm.ACCEPT_EDITS)
    assert json.loads(state_file.read_text(encoding="utf-8")) == {"mode": pm.ACCEPT_EDITS}


def test_cycle_mode_wraps_forwards():
    pm.set_mode(pm.MODES[0])
    seen = [pm.cycle_mode() for _ in range(len(pm.MODES))]
    assert seen == list(pm.MODES[1:]) + [pm.MODES[0]]
    assert pm.current_mode() == pm.MODES[0], "cycle_mode did not wrap"


def test_cycle_mode_wraps_backwards():
    pm.set_mode(pm.MODES[0])
    assert pm.cycle_mode(-1) == pm.MODES[-1]
    assert pm.current_mode() == pm.MODES[-1]
    seen = [pm.cycle_mode(-1) for _ in range(len(pm.MODES))]
    assert seen == list(reversed(pm.MODES[:-1])) + [pm.MODES[-1]]


def test_cycle_mode_returns_the_new_mode():
    pm.set_mode(pm.PLAN)
    assert pm.cycle_mode() == pm.MANUAL == pm.current_mode()


def test_cycle_mode_persists(state_file):
    pm.set_mode(pm.PLAN)
    new = pm.cycle_mode()
    assert json.loads(state_file.read_text(encoding="utf-8")) == {"mode": new}


# --- presentation ---------------------------------------------------------


@pytest.mark.parametrize("mode", pm.MODES)
def test_status_line_shows_the_label_and_only_shows_admin_when_elevated(mode):
    pm.set_mode(mode)
    pm.set_elevated(False)
    plain = pm.status_line()
    assert plain == pm.MODE_LABELS[mode]
    assert "+admin" not in plain

    pm.set_elevated(True, "why")
    assert pm.status_line() == pm.MODE_LABELS[mode] + " +admin"

    coloured = pm.status_line(colour=True)
    assert "\x1b[" in coloured and pm.MODE_LABELS[mode] in coloured
    assert str(pm.MODE_COLOURS[mode]) in coloured


@pytest.mark.parametrize("mode", pm.MODES)
def test_describe_renders_every_mode(mode):
    text = pm.describe(mode)
    assert pm.MODE_LABELS[mode] in text
    assert pm.MODE_BLURBS[mode] in text
    for risk in _RISK_CLASSES:
        assert pm._MATRIX[mode][risk] in text
    assert "privilege:" in text
    assert "no mode grants this" in text


def test_describe_reports_elevation_and_its_reason():
    pm.set_elevated(True, "signed driver install")
    text = pm.describe(pm.MANUAL)
    assert "elevated" in text
    assert "signed driver install" in text


def test_describe_defaults_to_the_active_mode():
    pm.set_mode(pm.AUTO)
    assert pm.describe() == pm.describe(pm.AUTO)


def test_describe_rejects_an_unknown_mode():
    with pytest.raises(ValueError):
        pm.describe("godmode")


@pytest.mark.parametrize("mode", pm.MODES)
def test_overview_lists_every_mode_and_marks_the_active_one(mode):
    pm.set_mode(mode)
    text = pm.overview()
    for other in pm.MODES:
        assert pm.MODE_LABELS[other] in text
        assert pm.MODE_BLURBS[other] in text
    marked = [line for line in text.splitlines()
              if line.strip().startswith(">")]
    assert len(marked) == 1, "exactly one mode should be marked active"
    assert pm.MODE_LABELS[mode] in marked[0]
    assert "destructive tools ask in every mode" in text


def test_overview_surfaces_elevation():
    pm.set_elevated(False)
    assert "PRIVILEGE" not in pm.overview()
    pm.set_elevated(True, "disk repair")
    text = pm.overview()
    assert "PRIVILEGE: elevated" in text
    assert "disk repair" in text


def test_labels_blurbs_and_colours_cover_every_mode():
    for table, name in ((pm.MODE_LABELS, "MODE_LABELS"),
                        (pm.MODE_BLURBS, "MODE_BLURBS"),
                        (pm.MODE_COLOURS, "MODE_COLOURS")):
        assert set(table) == set(pm.MODES), "%s does not cover MODES" % name


def test_host_is_elevated_returns_a_bool():
    assert isinstance(pm.host_is_elevated(), bool)


def test_host_elevation_is_independent_of_the_session_flag():
    """The process's real rights are not the session's privilege decision."""
    host = pm.host_is_elevated()
    pm.set_elevated(not host, "flip")
    assert pm.host_is_elevated() is host


# --- thread safety --------------------------------------------------------


def test_cycle_mode_under_concurrency_leaves_a_valid_mode():
    results = []
    errors = []
    barrier = threading.Barrier(8)

    def spin():
        try:
            barrier.wait(timeout=10)
            for _ in range(50):
                results.append(pm.cycle_mode())
        except Exception as exc:  # pragma: no cover - reported via assert
            errors.append(exc)

    threads = [threading.Thread(target=spin) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive(), "cycle_mode deadlocked"

    assert not errors, errors
    assert len(results) == 8 * 50
    bad = sorted({value for value in results if value not in pm.MODES})
    assert not bad, "cycle_mode returned torn state: %s" % bad
    assert pm.current_mode() in pm.MODES


def test_concurrent_decide_never_returns_an_unknown_action():
    actions = []
    barrier = threading.Barrier(4)

    def spin():
        barrier.wait(timeout=10)
        for _ in range(100):
            pm.cycle_mode()
            actions.append(pm.decide("file_delete").action)

    threads = [threading.Thread(target=spin) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()

    assert set(actions) <= {pm.ALLOW, pm.ASK, pm.DENY}
    assert pm.ALLOW not in actions, "file_delete was allowed outright in some mode"


# --- /elevate: the console command that actually turns the axis -----------
#
# set_elevated()/elevated()/elevation_reason() existed before this task but
# nothing reachable from the console called them. server.elevate() is that
# surface; command_catalog exposes it as "/elevate" automatically because it
# is a plain @mcp.tool() with no hand-written dispatch branch (see
# command_catalog's module docstring). These tests exercise the tool
# directly through `server`, on the same isolated state_file fixture the rest
# of this module uses, so they never touch a real permission_mode.json.


def test_elevate_with_no_argument_reports_current_state():
    import server

    assert pm.elevated() is False
    output = server.elevate()
    assert "elevation: off" in output
    assert "no /mode ever grants or revokes this" in output


def test_elevate_on_requires_a_reason():
    import server

    output = server.elevate(on="on")
    assert "reason" in output.lower()
    assert pm.elevated() is False, "a reason-less request must not elevate"


def test_elevate_on_with_a_reason_elevates_and_records_it():
    import server

    output = server.elevate(on="on", reason="installing a signed driver")
    assert pm.elevated() is True
    assert pm.elevation_reason() == "installing a signed driver"
    assert "elevation: on" in output
    assert "installing a signed driver" in output


def test_elevate_off_de_elevates_and_drops_the_reason():
    import server

    pm.set_elevated(True, "prior reason")
    output = server.elevate(on="off")
    assert pm.elevated() is False
    assert pm.elevation_reason() == ""
    assert "elevation: off" in output


@pytest.mark.parametrize("word", ["on", "true", "yes", "1", "ON", " On "])
def test_elevate_accepts_several_spellings_of_on(word):
    import server

    server.elevate(on=word, reason="test")
    assert pm.elevated() is True


@pytest.mark.parametrize("word", ["off", "false", "no", "0", "OFF"])
def test_elevate_accepts_several_spellings_of_off(word):
    import server

    pm.set_elevated(True, "prior")
    server.elevate(on=word)
    assert pm.elevated() is False


def test_elevate_rejects_an_unrecognised_argument():
    import server

    output = server.elevate(on="sudo")
    assert "unrecognised" in output.lower()
    assert pm.elevated() is False


def test_elevate_warns_when_the_host_process_is_not_actually_elevated(monkeypatch):
    import server

    monkeypatch.setattr(pm, "host_is_elevated", lambda: False)
    output = server.elevate(on="on", reason="test")
    assert "does not actually hold administrator" in output


def test_elevate_does_not_warn_when_the_host_process_is_actually_elevated(monkeypatch):
    import server

    monkeypatch.setattr(pm, "host_is_elevated", lambda: True)
    output = server.elevate(on="on", reason="test")
    assert "does not actually hold administrator" not in output


def test_elevate_on_is_refused_by_the_developer_gate_when_deployment_authenticates(monkeypatch):
    import server

    monkeypatch.setattr(
        server, "_developer_gate",
        lambda *_a, **_k: "refused: developer authority required.",
    )
    output = server.elevate(on="on", reason="test")
    assert output.startswith("refused:")
    assert pm.elevated() is False


def test_elevate_off_and_status_never_consult_the_developer_gate(monkeypatch):
    import server

    def boom(*_a, **_k):
        raise AssertionError("off/status must not require developer authority")

    monkeypatch.setattr(server, "_developer_gate", boom)
    pm.set_elevated(True, "prior")
    assert "elevation: on" in server.elevate()
    assert "elevation: off" in server.elevate(on="off")


def test_elevate_is_reachable_as_a_slash_command_and_classed_dangerous():
    import command_catalog

    command_catalog.reset_cache()
    command = command_catalog.by_name("/elevate")
    assert command is not None
    assert command.tool == "elevate"
    assert command.risk == "dangerous"


def test_elevate_never_allows_outright_in_any_mode():
    """Matches the module's own headline promise for the ``dangerous`` class."""
    for mode in pm.MODES:
        assert pm.decide("elevate", mode=mode).action != pm.ALLOW


def test_elevate_parses_a_natural_console_line():
    import command_catalog

    command_catalog.reset_cache()
    parsed = command_catalog.parse_invocation(
        '/elevate on "installing a driver"'
    )
    assert parsed == ("elevate", {"on": "on", "reason": "installing a driver"})
