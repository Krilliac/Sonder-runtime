"""``risk_of`` must not answer "I don't know" with a grade that means "allow".

Defect #18 (defect-sweep Shape A): a guard that, when it cannot do its job,
quietly does nothing instead of failing loudly. ``risk_of`` had three ignorance
branches -- an empty name, a blind catalog, and a name the catalog has never
heard of -- and *all three* resolved to a grade that
``decide(interactive=False)`` turns into ``allow``:

* the unknown-tool fallback returned ``"ask"``, which the non-interactive
  degrade allows in every mode but ``plan``;
* the empty-name branch returned ``"ask"`` under a comment saying
  "Fail closed";
* the ``CatalogUnavailable`` branch returned ``"dangerous"`` under a comment
  saying ``dangerous`` "is the one class that stops in every mode" -- which is
  true only interactively. ``dangerous`` is ``ask`` in ``manual``,
  ``acceptEdits`` and ``auto``, and the degrade allows it.

*Every* production gate passes ``interactive=False`` (``reloadable_mcp``,
``_agent_permission_gate_error``, ``_loop_permission_refusal``,
``_control_tool_refusal``, ``_http_tool_refusal``). So on the path that
actually runs, no severity of *grade* can fail closed -- the degrade is applied
after the grade is chosen. The fix is a grade the degrade does not apply to.

The anti-DoS half is asserted here too, and it is not decoration: a classifier
that refuses everything it cannot name is a denial of service on the runtime.
Work that legitimately fronts no registered tool must be *declared*
(``NON_TOOL_WORK``) rather than fall through the hole, so the exception is
visible and reviewable instead of silent.
"""
from __future__ import annotations

import pytest

import command_catalog
import permission_modes as pm

pytestmark = pytest.mark.unit

# The gates that actually run in production all pass this.
NON_INTERACTIVE = dict(interactive=False)

# Every mode, so "fails closed" cannot mean "fails closed only under plan".
ALL_MODES = (pm.PLAN, pm.MANUAL, pm.ACCEPT_EDITS, pm.AUTO)

UNCLASSIFIABLE = ("not_a_real_tool", "rm_rf_slash", "file_read_but_not_really")

# The grade the fix introduces, written as a literal rather than read off
# ``pm`` so these assertions fail on the *behaviour* at the parent commit
# instead of on a missing attribute.
UNCLASSIFIED = "unclassified"


@pytest.fixture(autouse=True)
def _sandbox(tmp_path, monkeypatch):
    """Per-test mode sandbox; mirrors ``test_permission_modes.state_file``."""
    monkeypatch.setattr(pm, "_state_path", lambda: str(tmp_path / "mode.json"))
    saved, saved_loaded = dict(pm._STATE), pm._LOADED
    with pm._LOCK:
        pm._STATE.update(mode=pm.DEFAULT_MODE, elevated=False, elevation_reason="")
    pm._LOADED = True
    monkeypatch.setattr(pm, "_rule_lookup", lambda _name: None)
    try:
        yield
    finally:
        with pm._LOCK:
            pm._STATE.clear()
            pm._STATE.update(saved)
        pm._LOADED = saved_loaded


# --- the defect itself ----------------------------------------------------


@pytest.mark.parametrize("mode", ALL_MODES)
@pytest.mark.parametrize("name", UNCLASSIFIABLE)
def test_an_unclassifiable_tool_is_refused_when_nobody_can_be_asked(name, mode):
    """The reproduction. Non-interactively, an unknown name was allowed."""
    decision = pm.decide(name, mode=mode, **NON_INTERACTIVE)
    assert decision.action == pm.DENY, (
        "%s in mode %s: an unclassifiable tool resolved to %r with nobody to "
        "ask, so the gate downgraded it instead of refusing it"
        % (name, mode, decision.action)
    )
    assert not decision.allowed


def test_an_unclassifiable_grade_is_distinguishable_from_a_catalogued_ask():
    """The two must not share a grade; conflating them is what hid the hole.

    A catalogued ``ask`` is a deliberate, written-down decision. The fallback
    is the classifier admitting defeat. They returned the same string, so no
    caller -- and no test -- could tell one from the other.
    """
    catalogued = next(
        t.name for t in _registered()
        if (command_catalog.by_name("/" + t.name) or None)
        and command_catalog.by_name("/" + t.name).risk == "ask"
        and pm.risk_of(t.name) == "ask"
    )
    assert pm.risk_of(catalogued) == "ask"
    assert pm.risk_of("not_a_real_tool") != pm.risk_of(catalogued), (
        "the unknown-tool fallback is indistinguishable from a catalogued ask"
    )


def test_a_deliberate_catalogued_ask_is_still_allowed_non_interactively():
    """The distinction must cut only one way. This is the 28-tool cohort.

    Ruled a documented deliberate decision, not a fallback. Fixing the
    fail-open must not quietly re-litigate it.
    """
    catalogued = next(
        t.name for t in _registered()
        if (command_catalog.by_name("/" + t.name) or None)
        and command_catalog.by_name("/" + t.name).risk == "ask"
        and pm.risk_of(t.name) == "ask"
    )
    assert pm.decide(catalogued, mode=pm.MANUAL, **NON_INTERACTIVE).allowed


def test_an_empty_tool_name_fails_closed_as_its_comment_claims():
    for mode in ALL_MODES:
        assert pm.decide("", mode=mode, **NON_INTERACTIVE).action == pm.DENY


def test_a_blind_catalog_stops_a_non_interactive_caller_in_every_mode(monkeypatch):
    """``CatalogUnavailable`` returned ``dangerous``, which the degrade allows."""
    def blind(_name):
        raise command_catalog.CatalogUnavailable("simulated registry failure")

    monkeypatch.setattr(command_catalog, "by_name", blind)
    for mode in ALL_MODES:
        decision = pm.decide("git_merge", mode=mode, **NON_INTERACTIVE)
        assert decision.action == pm.DENY, (
            "mode %s: the classifier was blind and the gate still allowed "
            "git_merge (%r)" % (mode, decision.action)
        )


def test_the_refusal_is_visible_and_says_why():
    """"Never silently downgraded" means the outcome has to be legible."""
    decision = pm.decide("not_a_real_tool", mode=pm.AUTO, **NON_INTERACTIVE)
    assert decision.risk == UNCLASSIFIED, decision.risk
    assert "classif" in decision.reason.lower(), decision.reason
    assert decision.source == "unclassified", decision.source


# --- the anti-DoS half ----------------------------------------------------


def test_a_person_at_a_console_is_asked_rather_than_refused():
    """Failing closed must not mean refusing a human who could just answer."""
    assert pm.decide("not_a_real_tool", mode=pm.MANUAL, interactive=True).action == pm.ASK


def test_declared_non_tool_work_is_still_permitted():
    """``sleep`` runs no tool at all and must keep working.

    It is the one name the census found on the fallback path, and refusing it
    would be the classifier DoSing a clamped ``time.sleep``. It is now
    declared rather than unclassifiable.
    """
    assert pm.risk_of("sleep") == "safe", (
        "sleep grades %r; it runs no tool, so it must be declared rather than "
        "fall through the unknown-tool hole" % pm.risk_of("sleep")
    )
    for mode in ALL_MODES:
        assert pm.decide("sleep", mode=mode, **NON_INTERACTIVE).allowed
    assert "sleep" in getattr(pm, "NON_TOOL_WORK", frozenset()), (
        "sleep must be declared in NON_TOOL_WORK, not graded by accident"
    )


def test_every_registered_tool_still_grades_and_none_is_unclassified():
    """The DoS check with teeth: no real tool may land on the new grade."""
    broken = sorted(t.name for t in _registered()
                    if pm.risk_of(t.name) == UNCLASSIFIED)
    assert broken == [], (
        "these registered tools became unclassifiable: %s" % ", ".join(broken)
    )


def test_the_new_grade_has_a_row_in_every_mode():
    for mode in ALL_MODES:
        assert UNCLASSIFIED in pm._MATRIX[mode], (
            "mode %s has no row for the unclassified grade, so _MATRIX.get "
            "would silently fall back" % mode
        )


# --- standing floor: the census, as a test -------------------------------


def test_no_bounded_gate_surface_carries_an_unclassifiable_name():
    """Measured, not estimated: every name each gate can hand ``risk_of``.

    This is the census that established the count for defect #18, kept as a
    check so a later branch cannot re-open the hole by adding an ungraded
    name. It is not vacuous -- ``test_...distinguishable...`` proves the
    detector fires, and the size assertions below prove the surfaces are
    populated rather than silently empty.
    """
    import server

    surfaces = {
        "mcp_registry": [t.name for t in _registered()],
        "console_slash": sorted({t for v in command_catalog.console_tools().values()
                                 for t in v}),
        "http_slash": sorted({t for v in command_catalog.http_slash_tools().values()
                              for t in v}),
    }
    for label, names in surfaces.items():
        assert len(names) > 50, "%s surface looks empty (%d)" % (label, len(names))
        ungraded = sorted(n for n in names if pm.risk_of(n) == UNCLASSIFIED)
        assert ungraded == [], (
            "%s hands risk_of names it cannot classify, which the gate then "
            "refuses: %s" % (label, ", ".join(ungraded))
        )
    assert server is not None


def test_non_tool_work_cannot_drift_into_a_free_floating_list():
    """Every declared exception must still be reachable and still front no tool.

    ``NON_TOOL_WORK`` is a hole punched in the fail-closed rule, so it needs
    the same discipline ``EXECUTION_COMMANDS`` gets: a name that stops being
    reachable is dead permission, and a name that acquires a real tool must be
    graded by the catalog rather than by this table.
    """
    import server

    reachable = _loop_action_types()
    assert len(reachable) > 10, "loop action census looks empty (%d)" % len(reachable)
    for name, grade in pm.NON_TOOL_WORK.items():
        assert grade in ("safe", "ask", "mutation", "execution", "dangerous"), name
        assert name in reachable, (
            "%s is declared non-tool work but no gate can reach it" % name
        )
        assert command_catalog.by_name("/" + name) is None, (
            "%s is a registered tool now; delete its NON_TOOL_WORK entry and "
            "let the catalog grade it" % name
        )
        assert server._loop_action_tool(name) == name, name


def _loop_action_types():
    """Literal ``action_type`` comparisons in ``server._loop_dispatch``."""
    import ast
    import inspect

    import server

    src = inspect.getsource(server)
    out = set()
    for node in ast.walk(ast.parse(src)):
        if not (isinstance(node, ast.FunctionDef) and node.name == "_loop_dispatch"):
            continue
        for sub in ast.walk(node):
            if not (isinstance(sub, ast.Compare) and isinstance(sub.left, ast.Name)):
                continue
            if sub.left.id != "action_type":
                continue
            for op, cmp in zip(sub.ops, sub.comparators):
                if not isinstance(op, (ast.Eq, ast.In)):
                    continue
                if isinstance(cmp, ast.Constant) and isinstance(cmp.value, str):
                    out.add(cmp.value)
                elif isinstance(cmp, (ast.Tuple, ast.List, ast.Set)):
                    out.update(e.value for e in cmp.elts
                               if isinstance(e, ast.Constant)
                               and isinstance(e.value, str))
    return out


def _registered():
    import server
    return list(server.mcp._tool_manager.list_tools())
