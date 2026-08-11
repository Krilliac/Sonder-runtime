"""``/permissions`` must show the *effective* decision, not just the rule.

Until the gate was wired in, ``permission_policy`` printed a rule list that
nothing consulted, so "what does it say" and "what will happen" could not
disagree -- there was no second opinion. Now there is: a mode decides too, and
the two compose by an explicit precedence (``permission_modes`` docstring).
Printing only the rule is therefore no longer merely incomplete, it is capable
of being wrong -- ``status`` shows ``allow`` while ``plan`` is in force and a
deny rule would beat ``auto`` outright.

These tests pin three things:

* the output names the active mode, so a reader knows which dial was applied;
* where a rule and the mode disagree, the output says which one governs, taken
  from ``Decision.source`` rather than re-derived here (re-deriving precedence
  in the renderer is the exact defect being fixed, one layer up);
* rendering is *pure display* -- no prompt, no elevation change, and exactly
  one read of ``permissions.json`` for the whole table.

That last one is not hygiene. ``permission_modes._default_rule_lookup`` re-reads
and re-parses ``permissions.json`` on every ``decide()`` call and resolves its
own home, so rendering a table through it would issue one read per row against
a file that can change mid-render, and would ignore the ``home`` under test.
"""
from __future__ import annotations

import builtins
import json
import pathlib

import pytest

import permission_modes as pm
import permission_rules
import server
import sonder_repl
from sonder_runtime.domain.execution import policy as execution_policy

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def mode_sandbox(tmp_path, monkeypatch):
    """A tmp mode file and a known starting mode, restored afterwards.

    ``set_mode`` persists to disk, so without this a test that selects ``auto``
    would change what every later test file decides.
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
            pm._STATE.clear()
            pm._STATE.update(saved)
        pm._LOADED = saved_loaded


@pytest.fixture(autouse=True)
def policy_home(tmp_path, monkeypatch):
    """Point every surface at a tmp home and re-arm the load warning."""
    monkeypatch.setattr(server.sonder_paths, "default_home", lambda: tmp_path)
    permission_rules.reset_load_warnings()
    yield
    permission_rules.reset_load_warnings()


def _write_policy(home, rules):
    path = home / "permissions.json"
    path.write_text(json.dumps(rules), encoding="utf-8")
    return path


def _row_for(text, pattern):
    """The listing row whose pattern column is exactly ``pattern``."""
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == pattern:
            return line
    raise AssertionError("no row for pattern %r in:\n%s" % (pattern, text))


# --- the output names the active mode --------------------------------------


def test_listing_names_the_active_mode(tmp_path):
    """A rule table that never says which mode is in force is half the answer.

    ``ask`` in the table means "manual will prompt" and "auto will not"; the
    reader cannot tell which without the mode being printed alongside.
    """
    _write_policy(tmp_path, [{"pattern": "status", "action": "allow"}])
    pm.set_mode(pm.ACCEPT_EDITS)

    out = server.permission_policy()

    assert "permission mode: accept edits" in out
    assert pm.MODE_BLURBS[pm.ACCEPT_EDITS] in out


def test_single_tool_form_names_the_mode_and_the_risk_class(tmp_path):
    _write_policy(tmp_path, [{"pattern": "status", "action": "allow"}])
    pm.set_mode(pm.MANUAL)

    out = server.permission_policy("status")

    # The two lines four existing assertions depend on, unchanged.
    assert "permission check: status" in out
    assert "  action: allow" in out
    assert "  mode: manual" in out
    assert "  risk: %s" % pm.risk_of("status") in out


# --- when the rule and the mode disagree, say which wins --------------------


def test_deny_rule_beats_a_mode_that_would_allow(tmp_path):
    """The sharpest disagreement: mode says allow outright, rule says deny.

    ``status`` is risk ``safe``, which every mode including ``auto`` allows, so
    the only thing that can refuse it is the rule. Printing the rule's ``deny``
    without saying it governs would leave a reader unable to tell this from a
    rule the mode had overridden.
    """
    _write_policy(tmp_path, [{"pattern": "status", "action": "deny",
                              "note": "operator lockout"}])
    pm.set_mode(pm.AUTO)
    assert pm._MATRIX[pm.AUTO][pm.risk_of("status")] == pm.ALLOW, (
        "fixture assumption: auto allows status outright"
    )

    out = server.permission_policy("status")

    assert "  effective: deny" in out
    assert "  governed by: rule" in out


def test_allow_rule_satisfies_the_modes_ask(tmp_path):
    """The other direction: the mode would prompt, the rule loosens it."""
    _write_policy(tmp_path, [{"pattern": "file_write", "action": "allow",
                              "note": "trusted in this workspace"}])
    pm.set_mode(pm.MANUAL)
    assert pm._MATRIX[pm.MANUAL][pm.risk_of("file_write")] == pm.ASK, (
        "fixture assumption: manual asks before file_write"
    )

    out = server.permission_policy("file_write")

    assert "  action: allow" in out          # the rule
    assert "  effective: allow" in out       # and it is what happens
    assert "  governed by: rule" in out


def test_mode_governs_when_no_rule_applies(tmp_path):
    """No rule matched, so the mode decided -- and must be credited, not the rule.

    The rule column here shows the permissive ``no matching rule`` fallback,
    which reads like an explicit ``ask`` unless the source says otherwise.
    """
    _write_policy(tmp_path, [{"pattern": "status", "action": "allow"}])
    pm.set_mode(pm.MANUAL)

    out = server.permission_policy("file_write")

    assert "  matched: %s" % execution_policy.NO_MATCH_RULE["pattern"] in out
    assert "  governed by: mode" in out


def test_plans_denial_is_not_credited_to_an_allow_rule(tmp_path):
    """``plan``'s denials are never overridden by a rule, and must not look it.

    An allow rule is inert under ``plan``. Showing the rule's ``allow`` with no
    effective verdict would tell an operator the opposite of what happens.
    """
    _write_policy(tmp_path, [{"pattern": "file_write", "action": "allow"}])
    pm.set_mode(pm.PLAN)

    out = server.permission_policy("file_write")

    assert "  action: allow" in out
    assert "  effective: deny" in out
    assert "  governed by: mode" in out


def test_elevation_denial_is_credited_to_privilege(tmp_path, monkeypatch):
    """A refusal for lack of elevation is neither the rule's nor the mode's."""
    monkeypatch.setattr(pm, "PRIVILEGED_TOOLS", frozenset({"status"}))
    _write_policy(tmp_path, [{"pattern": "status", "action": "allow"}])
    pm.set_mode(pm.AUTO)

    out = server.permission_policy("status")

    assert "  effective: deny" in out
    assert "  governed by: privilege" in out


# --- one effective verdict per glob row would be a new lie ------------------


def test_glob_rows_carry_no_effective_verdict(tmp_path):
    """``web_*`` covers tools of differing risk; one verdict for the row lies.

    The per-tool truth belongs to the single-tool form, which is asked about a
    name. A wildcard row is asked about a pattern, and the honest answer for a
    pattern is the rule alone.
    """
    _write_policy(tmp_path, [
        {"pattern": "web_*", "action": "ask", "note": "uses network access"},
        {"pattern": "status", "action": "allow", "note": "read-only"},
    ])
    pm.set_mode(pm.AUTO)

    out = server.permission_policy()

    assert "->" not in _row_for(out, "web_*")
    assert "-> allow" in _row_for(out, "status")


# --- rendering is pure display ---------------------------------------------


def test_rendering_the_listing_reads_the_policy_file_exactly_once(tmp_path, monkeypatch):
    """One read for the whole table, from the home under test.

    Rendering through ``permission_modes._default_rule_lookup`` would issue one
    read per row -- and would read the developer's real ``~/.sonder`` policy
    instead of this one, because that lookup resolves its own home.
    """
    policy = _write_policy(tmp_path, [dict(rule) for rule in execution_policy.DEFAULT_RULES])
    pm.set_mode(pm.MANUAL)

    reads = []
    real_read_text = pathlib.Path.read_text

    def counting_read_text(self, *args, **kwargs):
        if self.name == "permissions.json":
            reads.append(str(self))
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "read_text", counting_read_text)
    out = server.permission_policy()

    assert "sonder permission rules" in out
    assert reads == [str(policy)]


def test_rendering_performs_nothing_it_only_reports(tmp_path, monkeypatch):
    """Display must not prompt, must not persist, must not spend the axis.

    An earlier version of this test patched ``builtins.input`` and compared
    ``permission_modes._STATE`` before and after. It could not fail. Two
    mutations of ``server._permission_policy_text`` were run against it and it
    reported both as passes:

    * calling ``sonder_repl._confirm(...)``, which never reaches ``input()`` at
      all -- that helper short-circuits when stdin is not a tty, and pytest's
      never is. Nothing in ``server``, ``permission_rules`` or
      ``permission_modes`` imports ``sonder_repl`` either, so the tree's only
      ``input()`` was unreachable from the patch to begin with;
    * calling ``set_mode(current_mode())``, a same-value write-back that
      persists the mode file to disk and leaves ``_STATE`` byte-identical.

    A test that reads as protection and provides none is the exact defect this
    branch exists to remove, so this one patches the *acts* rather than their
    traces: every seam a performing render could reach for raises. The
    ``_STATE`` comparison is kept only as a backstop against a direct write to
    the dict, and is not what makes this a guard.
    """
    _write_policy(tmp_path, [dict(rule) for rule in execution_policy.DEFAULT_RULES])
    pm.set_mode(pm.MANUAL)
    pm.set_elevated(True, "installing a signed driver")
    before = dict(pm._STATE)

    def _performing(name):
        def _raise(*_args, **_kwargs):
            raise AssertionError("rendering the policy called %s" % name)
        return _raise

    # Prompting.
    monkeypatch.setattr(builtins, "input", _performing("input()"))
    monkeypatch.setattr(sonder_repl, "_confirm", _performing("sonder_repl._confirm()"))
    # Changing, or persisting, either axis.
    monkeypatch.setattr(pm, "set_mode", _performing("permission_modes.set_mode()"))
    monkeypatch.setattr(pm, "cycle_mode", _performing("permission_modes.cycle_mode()"))
    monkeypatch.setattr(
        pm, "set_elevated", _performing("permission_modes.set_elevated()"))
    monkeypatch.setattr(pm, "_save", _performing("permission_modes._save()"))
    # Writing back the policy it was asked to display.
    monkeypatch.setattr(permission_rules, "save", _performing("permission_rules.save()"))
    monkeypatch.setattr(
        permission_rules, "add_rule", _performing("permission_rules.add_rule()"))

    server.permission_policy()
    server.permission_policy("file_delete")

    assert dict(pm._STATE) == before


def test_elevation_and_its_reason_are_shown(tmp_path):
    """``/elevate``'s docstring promises the reason is shown by ``/permissions``.

    It was not, which made that sentence false. Rendering it here is what makes
    it true.
    """
    _write_policy(tmp_path, [{"pattern": "status", "action": "allow"}])
    pm.set_elevated(True, "installing a signed driver")

    out = server.permission_policy()

    assert "elevation: on" in out
    assert "installing a signed driver" in out


# --- the un-injected renderer is untouched ---------------------------------


def test_format_policy_without_a_decide_is_byte_identical(tmp_path):
    """Every other caller of the renderer must see exactly the old bytes.

    The effective verdict needs a mode and a rule snapshot that only the server
    surface has; a renderer called without them must not invent one.
    """
    _write_policy(tmp_path, [{"pattern": "file_delete", "action": "deny",
                              "note": "destructive by default"}])

    single = permission_rules.format_policy(tmp_path, "file_delete")
    assert single == "\n".join([
        "permission check: file_delete",
        "  action: deny",
        "  matched: file_delete",
        "  note: destructive by default",
    ])

    listing = permission_rules.format_policy(tmp_path)
    assert listing == "\n".join([
        "sonder permission rules",
        "  path: %s" % (tmp_path / "permissions.json"),
        "  " + "deny".ljust(5) + " " + "file_delete".ljust(32) + " "
        + "destructive by default",
    ])


def test_a_degraded_load_still_warns_through_the_effective_renderer(tmp_path):
    """The partial-load warning must survive the snapshot being passed in.

    A dropped deny rule is the failure the warning exists for, and it would be
    silently lost if the augmented path reported the load separately.
    """
    broken = [dict(rule) for rule in execution_policy.DEFAULT_RULES]
    for rule in broken:
        if rule["pattern"] == "file_delete":
            rule["action"] = "DENY!"
    _write_policy(tmp_path, broken)

    out = server.permission_policy()

    assert "WARNING:" in out
    assert "file_delete" in out


# --- the source is decided once, at the decision point ---------------------


def test_every_decide_return_site_names_its_source():
    """``Decision.source`` must be set at all five sites, not defaulted into.

    The renderer reads this instead of re-deriving the precedence, so a return
    site that forgot to set it would silently attribute a rule's or a
    privilege's refusal to the mode.
    """
    deny = lambda name: {"pattern": name, "action": "deny"}      # noqa: E731
    allow = lambda name: {"pattern": name, "action": "allow"}    # noqa: E731
    none = lambda name: None                                     # noqa: E731

    assert pm.decide("status", mode=pm.AUTO, rule_lookup=deny).source == "rule"
    assert pm.decide(
        "file_write", mode=pm.MANUAL, rule_lookup=allow).source == "rule"
    assert pm.decide(
        "status", mode=pm.AUTO, rule_lookup=none,
        requires_elevation=True).source == "privilege"
    assert pm.decide(
        "file_write", mode=pm.MANUAL, rule_lookup=none,
        interactive=False).source == "non-interactive"
    assert pm.decide(
        "file_write", mode=pm.MANUAL, rule_lookup=none).source == "mode"


def test_decision_source_is_carried_into_the_dict_form():
    decision = pm.decide("status", mode=pm.AUTO, rule_lookup=lambda n: None)
    assert decision.to_dict()["source"] == "mode"


# --- the caveat that makes an incomplete row recoverable -------------------
#
# `/permissions` renders with `interactive=True`, so `effective: ask` is the
# operator's answer and not an MCP caller's. That output ships because it is
# incomplete rather than false: one line, printed unconditionally on every
# render, states the exact rule a reader needs to get from the row to their
# own answer. That line is therefore load-bearing, and was untested -- make it
# conditional or move it and the gap becomes a plain falsehood.


def test_the_ask_caveat_is_printed_on_every_render(tmp_path):
    _write_policy(tmp_path, [{"pattern": "status", "action": "allow"}])

    for mode in (pm.PLAN, pm.MANUAL, pm.ACCEPT_EDITS, pm.AUTO):
        pm.set_mode(mode)
        assert pm.ASK_CAVEAT in server.permission_policy(), mode
        assert pm.ASK_CAVEAT in server.permission_policy("file_write"), mode


def test_the_caveat_states_the_rule_it_has_to_state(tmp_path):
    """Not just "a line is present" -- the two facts a reader needs.

    A caller with nobody to ask proceeds instead of being asked, and `plan` is
    the exception. Both are behaviour, so both are measured here rather than
    read off the sentence.
    """
    _write_policy(tmp_path, [{"pattern": "status", "action": "allow"}])
    pm.set_mode(pm.MANUAL)
    lookup = lambda _tool: None

    assert pm.decide(
        "file_write", interactive=True, mode=pm.MANUAL, rule_lookup=lookup,
    ).action == pm.ASK
    assert pm.decide(
        "file_write", interactive=False, mode=pm.MANUAL, rule_lookup=lookup,
    ).action == pm.ALLOW
    assert pm.decide(
        "file_write", interactive=False, mode=pm.PLAN, rule_lookup=lookup,
    ).action == pm.DENY

    out = server.permission_policy()
    assert "nobody to ask" in out
    assert "except under plan" in out


# --- an `ask` row says whose answer it is ---------------------------------


def test_an_ask_row_shows_both_callers_answers(tmp_path):
    """`ask` is the only verdict that differs between the two callers.

    Every other row is the same for an operator and for an MCP client -- a
    `deny` is a deny, an `allow` is an allow -- so naming both answers on the
    rows where they differ upgrades this output from disclosure to truth,
    without plumbing a caller flag through the renderer.
    """
    _write_policy(tmp_path, [{"pattern": "file_write", "action": "ask",
                              "note": "workspace writes"}])
    pm.set_mode(pm.MANUAL)

    single = server.permission_policy("file_write")
    assert "  effective: ask (console) / allow (non-interactive)" in single

    listing = server.permission_policy()
    assert "-> ask (mode) / allow (non-interactive)" in listing


def test_only_ask_rows_are_qualified(tmp_path):
    """A deny or an allow means the same thing to both callers; say it plainly."""
    _write_policy(tmp_path, [{"pattern": "file_delete", "action": "deny",
                              "note": "destructive by default"}])
    pm.set_mode(pm.MANUAL)

    out = server.permission_policy("file_delete")

    assert "  effective: deny" in out
    assert "non-interactive" not in out.split("permission mode:")[0]
