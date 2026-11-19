"""Approval owner binding, the fail-closed operator view, and two mode rules.

Each test pins one decision:

* with no owner scope bound the ledger shows and approves only the exact
  unowned ('') scope; the all-owners operator view exists only inside an
  explicit ``operator_scope``, which the console and the MCP entry bind after
  an admin-level check;
* approve and deny bind to the exact pending row's owner and digest; the owner
  is never inferred from the most recent row. Two owners with an identical
  pending call get one call id each (``<call id>@<owner tag>``), and approving
  one never approves the other;
* the console's ``overrode a denial`` notice and audit field come from the row
  ``issue`` actually cleared, not from a check of the bound owner;
* ``set_global_mode`` never loosens a session scope that is stricter than the
  new global mode;
* every deny rule holds in bypass, the shipped ``file_delete`` default too:
  bypass skips ``ask`` only.
"""
from __future__ import annotations

import hashlib
import json

import pytest

import permission_modes as pm
import server
from sonder_runtime.adapters.security import approval_ledger as ledger_module
from sonder_runtime.adapters.security.approval_ledger import ApprovalLedger
from sonder_runtime.domain.execution import policy as rule_policy
from sonder_runtime.interfaces.http.facades import approvals as facade

pytestmark = pytest.mark.unit

ARGS = {"path": "notes.txt", "content": "same for both"}


def _owner_of(principal: str) -> str:
    return "ap-" + hashlib.sha256(principal.encode("utf-8")).hexdigest()


@pytest.fixture(autouse=True)
def _isolated_mode_state(tmp_path, monkeypatch):
    saved_state = dict(pm._STATE)
    saved_scopes = dict(pm._SCOPED_MODES)
    saved_loaded = pm._LOADED
    state_file = tmp_path / "permission_mode.json"
    monkeypatch.setattr(pm, "_state_path", lambda: str(state_file))
    pm._reset_for_tests()
    yield
    with pm._LOCK:
        pm._STATE.clear()
        pm._STATE.update(saved_state)
        pm._SCOPED_MODES.clear()
        pm._SCOPED_MODES.update(saved_scopes)
        pm._LOADED = saved_loaded


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    store = ApprovalLedger(tmp_path / "approvals.db")
    monkeypatch.setattr(pm, "_approval_ledger", lambda: store)
    monkeypatch.delenv("SONDER_ALLOW_PERMISSION_EDITS", raising=False)
    return store


def _refuse(ledger, principal: str, args=None, tool="file_write"):
    digest = pm.call_digest(tool, args or ARGS)
    if principal:
        with pm.mode_scope(principal, "s1"):
            ledger.record_pending(tool, digest, surface="http", preview="notes")
    else:
        ledger.record_pending(tool, digest, surface="console", preview="notes")
    return tool, digest


def _spends(ledger, principal: str, tool: str, digest: str) -> bool:
    with pm.mode_scope(principal, "s1"):
        return ledger.consume(tool, digest, surface="http") is not None


# --- the operator view fails closed ---------------------------------------


def test_an_unbound_context_sees_and_approves_only_unowned_calls(ledger):
    tool, digest = _refuse(ledger, "account:alice")
    assert not ledger_module.operator_view()
    assert ledger.pending() == []
    with pytest.raises(ledger_module.ApprovalLedgerError):
        ledger.resolve_call(digest[:16])
    with pytest.raises(ledger_module.ApprovalLedgerError):
        ledger.deny(digest[:16])
    # An approval issued with no scope bound is the unowned caller's own.
    ledger.issue(tool, digest, approver="nobody")
    assert not _spends(ledger, "account:alice", tool, digest)
    with ledger_module.operator_scope():
        assert ledger_module.operator_view()
        assert [item.owner for item in ledger.pending()] == [_owner_of("account:alice")]


# --- approve and deny bind to the exact row -------------------------------


def test_identical_calls_from_two_owners_get_one_call_id_each(ledger):
    tool, digest = _refuse(ledger, "account:alice")
    _refuse(ledger, "account:bob")
    with ledger_module.operator_scope():
        refs = sorted(item.ref for item in ledger.pending())
        assert len(set(refs)) == 2 and all(ref.startswith(digest[:16] + "@ap-") for ref in refs)
        with pytest.raises(ledger_module.AmbiguousCallError) as caught:
            ledger.resolve_call(digest[:16])
        assert all(ref in str(caught.value) for ref in refs)
        alice_ref = next(item.ref for item in ledger.pending()
                         if item.owner == _owner_of("account:alice"))
        assert ledger.resolve_call(alice_ref).owner == _owner_of("account:alice")
        # Without a named owner the ledger never guesses one.
        with pytest.raises(ledger_module.AmbiguousCallError):
            ledger.issue(tool, digest, approver="admin")
    assert not _spends(ledger, "account:alice", tool, digest)
    assert not _spends(ledger, "account:bob", tool, digest)


def test_approving_one_owners_call_does_not_approve_the_others(ledger):
    tool, digest = _refuse(ledger, "account:alice")
    _refuse(ledger, "account:bob")  # the most recent row with that digest
    with ledger_module.operator_scope():
        alice = next(item for item in ledger.pending() if item.owner == _owner_of("account:alice"))
        result = facade.approve_call(ledger, alice.ref, {}, approver="admin", surface="http")
        assert result.status == 201, result.body
    assert not _spends(ledger, "account:bob", tool, digest)
    assert _spends(ledger, "account:alice", tool, digest)


def test_each_owner_of_an_identical_call_can_hold_its_own_open_approval(ledger):
    tool, digest = _refuse(ledger, "account:alice")
    _refuse(ledger, "account:bob")
    with ledger_module.operator_scope():
        for item in ledger.pending():
            result = facade.approve_call(ledger, item.ref, {}, approver="admin")
            assert result.status == 201, result.body
        bare = facade.approve_call(ledger, digest[:16], {}, approver="admin")
        assert bare.status == 409 and bare.body["error"]["code"] == "CALL_AMBIGUOUS"
    assert _spends(ledger, "account:alice", tool, digest)
    assert _spends(ledger, "account:bob", tool, digest)


def test_denying_one_owners_call_leaves_the_other_alone(ledger):
    tool, digest = _refuse(ledger, "account:alice")
    _refuse(ledger, "account:bob")
    with ledger_module.operator_scope():
        bob = next(item for item in ledger.pending() if item.owner == _owner_of("account:bob"))
        result = facade.deny_call(ledger, bob.ref, {})
        assert result.status == 200, result.body
        bare = facade.deny_call(ledger, digest[:16], {})
        assert bare.status == 409 and bare.body["error"]["code"] == "CALL_AMBIGUOUS"
    with pm.mode_scope("account:bob", "s1"):
        assert ledger.denied_for(tool, digest)
    with pm.mode_scope("account:alice", "s1"):
        assert not ledger.denied_for(tool, digest)


def test_issue_reports_the_denial_it_overrode_on_the_row_it_matched(ledger):
    tool, digest = _refuse(ledger, "account:alice")
    with ledger_module.operator_scope():
        pending = ledger.deny(digest[:16])
        approval = ledger.issue(tool, digest, approver="console", owner=pending.owner,
                                override_denial=True)
    assert approval.overrode_denial is True
    assert approval.owner == _owner_of("account:alice")
    assert _spends(ledger, "account:alice", tool, digest)


# --- the console and MCP entry: operator view after an admin-level check ---


def test_the_console_approves_an_account_call_and_says_it_overrode_a_denial(ledger, monkeypatch):
    seen = []
    monkeypatch.setattr(server, "_record_direct_tool",
                        lambda name, args, **kw: seen.append((name, dict(args))))
    tool, digest = _refuse(ledger, "account:alice")
    with ledger_module.operator_scope():
        ledger.deny(digest[:16])
    listing = server.control_command("/approvals", operator_approved=True)
    assert digest[:16] in listing
    out = server.control_command("/approve %s" % digest[:16], operator_approved=True)
    assert out.startswith("overrode a denial: approved file_write call"), out
    assert seen and seen[-1][1].get("overrode_denial") is True
    assert _spends(ledger, "account:alice", tool, digest)


def test_mcp_approval_of_an_account_call_needs_an_admin_token(ledger, monkeypatch):
    accounts = {"dev-token": {"username": "dev", "role": "developer"},
                "admin-token": {"username": "root", "role": "admin"}}
    monkeypatch.setattr(server, "_admin_account_from_token", lambda token: accounts.get(token))
    tool, digest = _refuse(ledger, "account:alice")
    # A developer token may approve, but only in the unowned view.
    out = server.permission_approve(call_id=digest[:16], token="dev-token")
    assert out.startswith("ERROR: no pending call"), out
    assert digest[:16] not in server.permission_approvals(token="dev-token")
    assert digest[:16] not in server.permission_approvals()
    # The env opt-in is not an admin-level check either.
    monkeypatch.setenv("SONDER_ALLOW_PERMISSION_EDITS", "1")
    assert server.permission_approve(call_id=digest[:16]).startswith("ERROR: no pending call")
    monkeypatch.delenv("SONDER_ALLOW_PERMISSION_EDITS")
    assert digest[:16] in server.permission_approvals(token="admin-token")
    out = server.permission_approve(call_id=digest[:16], token="admin-token")
    assert out.startswith("approved file_write call"), out
    assert _spends(ledger, "account:alice", tool, digest)


def test_mcp_approval_by_arguments_binds_the_one_pending_owner_or_refuses(ledger, monkeypatch):
    monkeypatch.setattr(server, "_admin_account_from_token",
                        lambda token: {"username": "root", "role": "admin"} if token == "a" else None)
    tool, digest = _refuse(ledger, "account:alice")
    out = server.permission_approve(tool=tool, arguments_json=json.dumps(ARGS), token="a")
    assert out.startswith("approved file_write call"), out
    assert _spends(ledger, "account:alice", tool, digest)
    _refuse(ledger, "account:alice")
    _refuse(ledger, "account:bob")
    out = server.permission_approve(tool=tool, arguments_json=json.dumps(ARGS), token="a")
    assert out.startswith("ERROR:") and "more than one owner" in out, out
    assert not _spends(ledger, "account:alice", tool, digest)
    assert not _spends(ledger, "account:bob", tool, digest)


# --- set_global_mode never loosens a stricter scope -----------------------


def test_set_global_mode_keeps_a_session_that_is_stricter_than_the_new_mode():
    with pm.mode_scope("account:bob", "s1"):
        pm.set_mode(pm.PLAN)
    with pm.mode_scope("account:carol", "s1"):
        pm.set_mode(pm.ACCEPT_EDITS)
    with pm.mode_scope("account:dave", "s1"):
        pm.set_mode(pm.BYPASS)
    assert pm.set_global_mode(pm.MANUAL) == pm.MANUAL
    with pm.mode_scope("account:bob", "s1"):
        assert pm.current_mode() == pm.PLAN
    with pm.mode_scope("account:carol", "s1"):
        assert pm.current_mode() == pm.MANUAL
    with pm.mode_scope("account:dave", "s1"):
        assert pm.current_mode() == pm.MANUAL
    pm.set_global_mode(pm.AUTO)
    with pm.mode_scope("account:bob", "s1"):
        assert pm.current_mode() == pm.PLAN
    with pm.mode_scope("account:carol", "s1"):
        assert pm.current_mode() == pm.AUTO


# --- every deny rule holds in bypass --------------------------------------


def test_bypass_keeps_the_shipped_file_delete_deny():
    shipped = next(dict(rule) for rule in rule_policy.DEFAULT_RULES
                   if rule["pattern"] == "file_delete")
    assert shipped["action"] == "deny"
    for interactive in (False, True):
        decision = pm.decide("file_delete", interactive=interactive, mode=pm.BYPASS,
                             rule_lookup=lambda _n: dict(shipped))
        assert decision.action == pm.DENY and decision.source == "rule", interactive
