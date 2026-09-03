"""Granting durable authority must not degrade to "yes" when nobody is asked.

Task #57. ``dangerous`` is ASK in manual/acceptEdits/auto, and every
non-interactive surface passes ``interactive=False``, where ASK degrades to
ALLOW. Measured on this branch before the fix, with no operator rules::

    tool                   risk          plan   manual  acceptEdits  auto
    admin_login            ask           deny   allow   allow        allow
    admin_set_account      dangerous     deny   allow   allow        allow
    admin_register         dangerous     deny   allow   allow        allow
    elevate                dangerous     deny   allow   allow        allow
    permission_rule_set    dangerous     deny   allow   allow        allow

Task #18 made ``UNCLASSIFIED`` non-degradable and left ``dangerous`` degradable.
For an elevation primitive that ordering is inverted: the grade for "we do not
know what this is" refuses to relax, while the grade for "this is the most
dangerous thing we have" still does.

Why a NARROW class and not all of ``dangerous``
------------------------------------------------
Measured: the catalog grades 19 commands ``dangerous``, and 8 of them are
reachable from ``server._agent_dispatch`` -- ``file_delete``,
``git_cherry_pick``, ``git_merge``, ``memory_privacy_repair``,
``memory_quality_repair``, ``self_heal_repair``, ``sqlite_mutate``,
``task_delete``. Making the whole grade non-degradable would refuse every one
of those in every non-interactive lane, which is the agent and autopilot lanes
entirely. That is not a gate, it is a shutdown, so the class is drawn around
what the degrade's own justification stops covering instead.

The degrade trades an unanswerable prompt for "this can be undone afterwards".
For privilege that becomes "the operator can revoke it later", which assumes
the operator *knows it happened*. A tool that grants durable authority hands
out something that outlives the call and leaves no prompt behind to have been
noticed, so the assumption the trade rests on is exactly what is at stake --
the same argument the ``/selfmod`` lane makes for self-modification.

``permission_rule_set`` is in the class because without it the class does not
bind: ``decide()`` resolves an explicit ALLOW rule at step 3, *before* the
non-interactive degrade, so a caller that can write rules unattended can write
one for ``admin_register`` and walk straight through. Verified in
``test_an_allow_rule_still_satisfies_the_ask``.

Both routes out survive, and are tested: an operator at a console reaches
``decide()`` with ``interactive=True`` and is asked; an explicit ``allow`` rule
resolves before this branch. A refusal with no route out is one operators learn
to route around.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import permission_modes as pm  # noqa: E402


@pytest.fixture(autouse=True)
def no_operator_rules(monkeypatch):
    monkeypatch.setattr(pm, "_rule_lookup", lambda name: None)


NON_INTERACTIVE_MODES = (pm.MANUAL, pm.ACCEPT_EDITS, pm.AUTO)


# --- the class exists and is declared, not inferred ------------------------

def test_the_class_is_declared_and_names_the_authority_granting_tools():
    assert pm.DURABLE_AUTHORITY_TOOLS == frozenset({
        "admin_login", "admin_register", "admin_set_account",
        "elevate", "permission_rule_set", "permission_approve",
    })


def test_every_member_is_a_real_registered_tool():
    """A class of strings nothing dispatches is a comment, not a gate."""
    import server
    for name in pm.DURABLE_AUTHORITY_TOOLS:
        assert hasattr(server, name), name


# --- the fix ---------------------------------------------------------------

# Every member the catalog grades `dangerous`, which is ASK in all three
# non-interactive modes -- so the degrade branch is reached and the class binds.
GRADED_DANGEROUS = sorted({
    "admin_register", "admin_set_account", "elevate", "permission_rule_set",
    "permission_approve",
})


@pytest.mark.parametrize("mode", NON_INTERACTIVE_MODES)
@pytest.mark.parametrize("tool", GRADED_DANGEROUS)
def test_durable_authority_is_refused_with_nobody_to_ask(tool, mode):
    decision = pm.decide(tool, interactive=False, mode=mode)
    assert decision.action == pm.DENY, "%s in %s" % (tool, mode)
    assert decision.source == "durable-authority"


def test_admin_login_binds_only_where_its_grade_reaches_the_degrade():
    """A measured cross-lane dependency, recorded rather than worked around.

    ``admin_login`` is in the class -- it is the only way
    ``sonder_repl.CURRENT_TOKEN`` becomes non-empty -- but on THIS lineage the
    catalog grades it ``ask``, and ``acceptEdits``/``auto`` map ``ask`` to ALLOW
    outright. The mode allows it before the non-interactive degrade is ever
    reached, so a class that only disarms the degrade cannot bind it there.
    Only ``manual`` (ask) and ``plan`` (deny) reach a branch at all.

    Regrading it is ``work/25-login-grading``'s fix (``92e177c`` adds
    ``admin_login`` to ``command_catalog._DANGEROUS``, with a measured
    justification: the same ``file_write`` to a protected control-plane path is
    refused before ``/login`` and succeeds after). Duplicating a 199-line
    catalog change here would put two rival versions of it in the merge, so
    this lane does not. When that lane lands, this test's first branch flips to
    the ``dangerous`` path and the class binds ``admin_login`` in all three
    modes with no change needed here.
    """
    grade = pm.risk_of("admin_login")
    assert grade in ("ask", "dangerous")
    assert pm.decide("admin_login", interactive=False, mode=pm.MANUAL).source == (
        "durable-authority"
    )
    if grade == "dangerous":
        for mode in NON_INTERACTIVE_MODES:
            decision = pm.decide("admin_login", interactive=False, mode=mode)
            assert decision.action == pm.DENY, mode
            assert decision.source == "durable-authority"
    else:
        # The measured state on this lineage. Not an endorsement: it is here so
        # the gap is visible and so the merge that closes it is detected.
        for mode in (pm.ACCEPT_EDITS, pm.AUTO):
            assert pm.decide(
                "admin_login", interactive=False, mode=mode,
            ).action == pm.ALLOW, mode


def test_plan_still_denies_them_for_its_own_reason():
    for tool in sorted(pm.DURABLE_AUTHORITY_TOOLS):
        decision = pm.decide(tool, interactive=False, mode=pm.PLAN)
        assert decision.action == pm.DENY
        assert decision.source == "mode", tool


def test_the_refusal_says_how_to_proceed():
    decision = pm.decide("admin_register", interactive=False, mode=pm.AUTO)
    assert "console" in decision.reason
    assert "allow rule" in decision.reason or "/permissions" in decision.reason


# --- both routes out survive ----------------------------------------------

@pytest.mark.parametrize("mode", NON_INTERACTIVE_MODES)
def test_an_operator_at_a_console_is_still_asked_not_refused(mode):
    """``interactive=True`` is what ``sonder_repl._console_has_operator()``
    passes at a TTY. The point is a prompt, not a wall.

    Scoped to the members the catalog grades ``dangerous``; ``admin_login``'s
    grade is the subject of ``test_admin_login_binds_only_where_its_grade_
    reaches_the_degrade``."""
    for tool in GRADED_DANGEROUS:
        decision = pm.decide(tool, interactive=True, mode=mode)
        assert decision.action == pm.ASK, "%s in %s" % (tool, mode)


def test_an_allow_rule_still_satisfies_the_ask(monkeypatch):
    """Step 3 resolves before the degrade, so a written-down, auditable
    exemption still works -- and this is also why ``permission_rule_set``
    itself has to be in the class, or the class is two calls from defeated."""
    monkeypatch.setattr(pm, "_rule_lookup", lambda name: (
        {"action": "allow", "pattern": name}
        if name == "admin_register" else None
    ))
    decision = pm.decide("admin_register", interactive=False, mode=pm.AUTO)
    assert decision.action == pm.ALLOW
    assert decision.source == "rule"


def test_an_explicit_deny_rule_still_outranks_everything(monkeypatch):
    monkeypatch.setattr(pm, "_rule_lookup", lambda name: (
        {"action": "deny", "pattern": name} if name == "elevate" else None
    ))
    decision = pm.decide("elevate", interactive=False, mode=pm.AUTO)
    assert decision.action == pm.DENY
    assert decision.source == "rule"


# --- the cost is bounded: the rest of `dangerous` is deliberately untouched -

@pytest.mark.parametrize("tool", [
    "file_delete", "git_merge", "git_cherry_pick", "sqlite_mutate",
    "task_delete", "self_heal_repair", "memory_quality_repair",
    "memory_privacy_repair",
])
def test_agent_reachable_dangerous_tools_are_refused_unattended(tool):
    """These 8 are the measured intersection of the catalog's ``dangerous``
    set with ``_agent_dispatch``'s dispatch names. The durable-authority class
    stays narrow on purpose, but these no longer degrade either: every
    ``dangerous`` tool is refused for a caller nobody can ask by the general
    unattended rule, under its own source, so the audit trail still tells
    "grants authority" apart from "is destructive"."""
    assert pm.risk_of(tool) == "dangerous"
    decision = pm.decide(
        tool, interactive=False, mode=pm.AUTO, rule_lookup=lambda _t: None,
    )
    assert decision.action == pm.DENY
    assert decision.source == "unattended"


def test_ordinary_ask_tools_are_untouched():
    for tool in ("admin_whoami", "admin_accounts", "file_read", "memory_search"):
        assert pm.decide(tool, interactive=False, mode=pm.AUTO).action == pm.ALLOW


def test_unclassified_still_refuses_for_its_own_reason():
    """Task #18's guard must keep its own source string, so the audit trail
    still distinguishes "nothing could classify this" from "this grants
    authority"."""
    decision = pm.decide("__no_such_tool__", interactive=False, mode=pm.AUTO)
    assert decision.action == pm.DENY
    assert decision.source == "unclassified"
