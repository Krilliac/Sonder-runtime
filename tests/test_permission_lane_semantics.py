"""Permission and approval semantics: bypass scope, HTTP mode scope, deny rules,
operator approvals across owners, and the non-stream client handoff contract.

Each test pins one decision:

* bypass is never saved to disk, for the global mode or for any scope;
* ``POST /v1/permission-mode`` changes the process-global mode for anything
  but bypass (the administrator's safety stop reaches every surface), while
  bypass stays scoped to the caller's principal and session;
* bypass skips prompts only: every deny rule (one the operator wrote, a
  shipped default, the degraded-policy deny) still refuses;
* the explicit operator scope (which the console and the MCP entry bind after
  an admin-level check) sees and approves every pending call, including
  account turns', and the approval is spent by the account that asked; an
  account sees only its own;
* a non-stream client handoff is a one-way handoff: its result endpoint stays
  open for ``DEFAULT_NON_STREAM_HANDOFF_TTL_SECONDS`` and the model is told the
  result does not come back in this turn.
"""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

import permission_modes as pm
from sonder_runtime.adapters.security import approval_ledger as ledger_module
from sonder_runtime.adapters.security.approval_ledger import ApprovalLedger
from sonder_runtime.domain.execution import policy as rule_policy
from sonder_runtime.interfaces.http import chat_tool_stream

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolated_mode_state(tmp_path, monkeypatch):
    saved_state = dict(pm._STATE)
    saved_scopes = dict(pm._SCOPED_MODES)
    saved_loaded = pm._LOADED
    state_file = tmp_path / "permission_mode.json"
    monkeypatch.setattr(pm, "_state_path", lambda: str(state_file))
    pm._reset_for_tests()
    yield state_file
    with pm._LOCK:
        pm._STATE.clear()
        pm._STATE.update(saved_state)
        pm._SCOPED_MODES.clear()
        pm._SCOPED_MODES.update(saved_scopes)
        pm._LOADED = saved_loaded


def _restart() -> None:
    """Drop every in-memory mode, as a new process would, and reload from disk."""
    pm._reset_for_tests()


def _owner_of(principal: str) -> str:
    return "ap-" + hashlib.sha256(principal.encode("utf-8")).hexdigest()


# --- bypass is never persisted --------------------------------------------


def test_scoped_bypass_is_not_saved_and_does_not_survive_a_restart(_isolated_mode_state):
    state_file = _isolated_mode_state
    with pm.mode_scope("account:bob", ""):
        pm.set_mode(pm.BYPASS)
        assert pm.current_mode() == pm.BYPASS
    with pm.mode_scope("api-key", "s1"):
        pm.set_mode(pm.BYPASS)
    with pm.mode_scope("local-open", "s1"):
        with pytest.raises(ValueError):
            pm.set_mode(pm.BYPASS)

    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert pm.BYPASS not in json.dumps(saved)

    _restart()
    for principal, session in (("account:bob", ""), ("account:bob", "other"), ("api-key", "s1"), ("local-open", "s1")):
        with pm.mode_scope(principal, session):
            assert pm.current_mode() == pm.DEFAULT_MODE
            decision = pm.decide("file_delete", interactive=False, rule_lookup=lambda _n: None)
            assert decision.action == pm.DENY


def test_a_bypass_scope_already_on_disk_is_not_restored(_isolated_mode_state):
    state_file = _isolated_mode_state
    state_file.write_text(json.dumps({
        "mode": pm.MANUAL,
        "scopes": {"account:bob|": pm.BYPASS, "account:carol|s": pm.PLAN},
    }), encoding="utf-8")
    _restart()
    with pm.mode_scope("account:bob", "any"):
        assert pm.current_mode() == pm.MANUAL
    with pm.mode_scope("account:carol", "s"):
        assert pm.current_mode() == pm.PLAN


def test_non_bypass_scopes_still_persist_next_to_an_in_memory_bypass(_isolated_mode_state):
    state_file = _isolated_mode_state
    with pm.mode_scope("account:carol", "s"):
        pm.set_mode(pm.PLAN)
    with pm.mode_scope("account:bob", "s"):
        pm.set_mode(pm.BYPASS)
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved["scopes"] == {"account:carol|s": pm.PLAN}
    # In memory the bypass scope still holds for this process.
    with pm.mode_scope("account:bob", "s"):
        assert pm.current_mode() == pm.BYPASS


def test_a_refused_raise_in_a_fresh_scope_keeps_it_following_the_global_mode(monkeypatch):
    pm.set_mode(pm.MANUAL)

    def refuse(*_args, **_kwargs):
        raise OSError(28, "No space left on device")

    real_replace = pm.os.replace
    monkeypatch.setattr(pm.os, "replace", refuse)
    with pm.mode_scope("account:bob", "s1"), pytest.raises(pm.ModePersistenceError):
        pm.set_mode(pm.AUTO)
    # Restore only os.replace: monkeypatch.undo() would also drop the
    # fixture's _state_path patch and write to the session's state home.
    monkeypatch.setattr(pm.os, "replace", real_replace)
    assert "account:bob|s1" not in pm._SCOPED_MODES
    pm.set_mode(pm.PLAN)
    with pm.mode_scope("account:bob", "s1"):
        assert pm.current_mode() == pm.PLAN


# --- HTTP mode changes ----------------------------------------------------


class _Handler:
    def __init__(self, headers=None):
        self.headers = dict(headers or {})
        self.sent: list[tuple[dict, int]] = []

    def _send_json_payload(self, payload, status=200):
        self.sent.append((dict(payload), status))


def _post_mode(body, *, principal="account:admin", headers=None):
    handler = _Handler(headers)
    chat_tool_stream.handle_permission_mode_post(
        handler, body, {"authorized": True, "mode": "account"},
        permission_policy=pm,
        principal_of=lambda _ctx: principal,
        idempotent_action=lambda _ctx, _key, _text, action: action(),
        send_idempotency_refusal=lambda _h, _r: False,
        observe_state=lambda: None,
        mode_data=lambda: {"mode": pm.current_mode()},
        approver_of=lambda _ctx: "admin",
    )
    return handler.sent[-1]


def test_http_non_bypass_mode_change_is_process_global():
    pm.set_mode(pm.AUTO)
    payload, status = _post_mode({"mode": "plan"})
    assert status == 200 and payload["mode"] == pm.PLAN
    # Unscoped surfaces (MCP, loops, autopilot) and every other caller see it.
    assert pm.current_mode() == pm.PLAN
    with pm.mode_scope("account:bob", "s1"):
        assert pm.current_mode() == pm.PLAN


def test_http_global_change_is_a_safety_stop_for_scoped_bypass():
    with pm.mode_scope("account:bob", "s1"):
        pm.set_mode(pm.BYPASS)
    with pm.mode_scope("account:admin", "mine"):
        pm.set_mode(pm.AUTO)
    payload, status = _post_mode({"mode": "manual"}, headers={"X-Sonder-Session": "mine"})
    assert status == 200 and payload["mode"] == pm.MANUAL
    with pm.mode_scope("account:bob", "s1"):
        assert pm.current_mode() == pm.MANUAL
    with pm.mode_scope("account:admin", "mine"):
        assert pm.current_mode() == pm.MANUAL


def test_http_bypass_stays_scoped_to_the_callers_session():
    payload, status = _post_mode(
        {"mode": "bypass", "confirm_bypass": True}, headers={"X-Sonder-Session": "chat-1"},
    )
    assert status == 200 and payload["mode"] == pm.BYPASS
    assert pm.current_mode() == pm.DEFAULT_MODE
    with pm.mode_scope("account:admin", "chat-2"):
        assert pm.current_mode() == pm.DEFAULT_MODE
    with pm.mode_scope("account:admin", "chat-1"):
        assert pm.current_mode() == pm.BYPASS


def test_global_mode_setter_never_sets_bypass():
    with pytest.raises(ValueError):
        pm.set_global_mode(pm.BYPASS)
    assert pm.current_mode() == pm.DEFAULT_MODE


# --- bypass and deny rules ------------------------------------------------


def _shipped(pattern):
    return next(dict(rule) for rule in rule_policy.DEFAULT_RULES if rule["pattern"] == pattern)


@pytest.mark.parametrize("tool", ["file_delete", "file_write", "run_code", "web_fetch"])
def test_bypass_keeps_an_explicit_operator_deny(tool):
    rule = {"pattern": tool, "action": "deny", "note": ""}
    for interactive in (False, True):
        decision = pm.decide(tool, interactive=interactive, mode=pm.BYPASS,
                             rule_lookup=lambda _n, r=rule: r)
        assert decision.action == pm.DENY, (tool, interactive)
        assert decision.source == "rule"


def test_bypass_keeps_the_shipped_default_denies_too():
    shipped = _shipped("file_delete")
    # Shipped file_delete is ask (always-ask per L16): refused unattended even in bypass.
    decision = pm.decide("file_delete", interactive=False, mode=pm.BYPASS,
                         rule_lookup=lambda _n: dict(shipped))
    assert decision.action == pm.DENY
    assert pm.decide("file_delete", interactive=True, mode=pm.BYPASS,
                     rule_lookup=lambda _n: dict(shipped)).action == pm.ASK
    # The same shipped row refuses outside bypass.
    assert pm.decide("file_delete", interactive=False, mode=pm.AUTO,
                     rule_lookup=lambda _n: dict(shipped)).action == pm.DENY
    # The chain-of-thought guard is a shipped deny that bypass never skips.
    cot = _shipped("admin_private_chain_of_thought")
    assert pm.decide("admin_private_chain_of_thought", interactive=True, mode=pm.BYPASS,
                     rule_lookup=lambda _n: dict(cot)).action == pm.DENY
    assert pm.decide("admin_private_chain_of_thought", interactive=False, mode=pm.BYPASS,
                     rule_lookup=lambda _n: dict(cot)).source == "rule"


def test_bypass_keeps_the_degraded_policy_deny():
    degraded = {"pattern": "<degraded permission policy>", "action": "deny",
                "note": "permission policy could not be enforced safely"}
    decision = pm.decide("file_write", interactive=False, mode=pm.BYPASS,
                         rule_lookup=lambda _n: dict(degraded))
    assert decision.action == pm.DENY and decision.source == "rule"


def test_bypass_does_not_run_an_unclassified_tool_unattended():
    decision = pm.decide("totally_unknown_tool_xyz", interactive=False, mode=pm.BYPASS,
                         rule_lookup=lambda _n: None)
    assert decision.action == pm.DENY and decision.source == "unclassified"


# --- operator approvals across owners -------------------------------------


@pytest.fixture
def ledger(tmp_path):
    return ApprovalLedger(tmp_path / "approvals.db")


def _account_refusal(ledger, principal="account:dev", tool="file_write", args=None):
    args = args or {"path": "notes.txt", "content": "x"}
    digest = pm.call_digest(tool, args)
    with pm.mode_scope(principal, "s1"):
        ledger.record_pending(tool, digest, surface="http", preview="notes")
    return tool, digest


def test_console_sees_and_approves_an_account_call_and_the_account_spends_it(ledger):
    tool, digest = _account_refusal(ledger)
    # Console / MCP: the operator scope they bind after an admin-level check.
    with ledger_module.operator_scope():
        listed = ledger.pending()
        assert [item.digest for item in listed] == [digest]
        assert listed[0].owner == _owner_of("account:dev")
        pending = ledger.resolve_call(digest[:16])
        approval = ledger.issue(pending.tool, pending.digest, approver="console",
                                surface="console", owner=pending.owner)
    assert "open" == approval.state(approval.issued_ts)
    with pm.mode_scope("account:dev", "s1"):
        assert ledger.open_for(tool, digest)
        spent = ledger.consume(tool, digest, surface="http")
    assert spent is not None and spent.nonce == approval.nonce
    # Another account cannot spend it.
    assert ledger.consume(tool, digest) is None


def test_console_approval_by_tool_and_arguments_reaches_the_account(ledger):
    tool, digest = _account_refusal(ledger)
    with ledger_module.operator_scope():
        ledger.issue(tool, digest, approver="console", surface="console")
    with pm.mode_scope("account:dev", "s1"):
        assert ledger.consume(tool, digest, surface="http") is not None


def test_console_can_deny_and_revoke_account_calls(ledger):
    tool, digest = _account_refusal(ledger)
    with ledger_module.operator_scope():
        approval = ledger.issue(tool, digest, approver="console")
        assert ledger.revoke(approval.nonce) is not None
        denied = ledger.deny(digest[:16])
    assert denied.owner == _owner_of("account:dev")
    with pm.mode_scope("account:dev", "s1"):
        assert ledger.denied_for(tool, digest)


def test_operator_scope_sees_every_owner_and_an_account_only_its_own(ledger):
    _tool, dev_digest = _account_refusal(ledger, "account:dev")
    _tool, eve_digest = _account_refusal(ledger, "account:eve", args={"path": "eve.txt"})
    with ledger.scope("", all_owners=True):
        assert {item.digest for item in ledger.pending()} == {dev_digest, eve_digest}
        ledger.issue("file_write", eve_digest, approver="admin", surface="http")
    with pm.mode_scope("account:dev", "s1"):
        assert [item.digest for item in ledger.pending()] == [dev_digest]
        with pytest.raises(ledger_module.ApprovalLedgerError):
            ledger.resolve_call(eve_digest[:16])
        assert ledger.approvals() == []
    with pm.mode_scope("account:eve", "s1"):
        assert ledger.consume("file_write", eve_digest) is not None


def test_developer_scope_keeps_own_plus_unowned(ledger):
    _tool, dev_digest = _account_refusal(ledger, "account:dev")
    _account_refusal(ledger, "account:eve", args={"path": "eve.txt"})
    with ledger.scope(_owner_of("account:dev"), include_unowned=True):
        assert [item.digest for item in ledger.pending()] == [dev_digest]


def test_same_call_from_console_and_account_resolves_to_each_callers_own_row(ledger):
    tool, digest = _account_refusal(ledger)
    ledger.record_pending(tool, digest, surface="console")  # the console's own refusal
    pending = ledger.resolve_call(digest[:16])
    assert pending.owner == ""
    with pm.mode_scope("account:dev", "s1"):
        assert ledger.resolve_call(digest[:16]).owner == _owner_of("account:dev")
    # The operator sees both rows: the bare id is ambiguous, each ref names one.
    with ledger_module.operator_scope():
        with pytest.raises(ledger_module.AmbiguousCallError):
            ledger.resolve_call(digest[:16])
        assert ledger.resolve_call(digest[:16] + "@-").owner == ""
        dev_ref = ledger_module.call_ref(digest, _owner_of("account:dev"))
        assert ledger.resolve_call(dev_ref).owner == _owner_of("account:dev")


def test_owner_migration_survives_an_orphaned_rebuild_table(tmp_path):
    import sqlite3

    path = tmp_path / "approvals.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE pending_calls (
            digest TEXT PRIMARY KEY, tool TEXT NOT NULL, surface TEXT NOT NULL DEFAULT '',
            preview TEXT NOT NULL DEFAULT '', first_ts REAL NOT NULL, last_ts REAL NOT NULL,
            count INTEGER NOT NULL DEFAULT 1, detail TEXT NOT NULL DEFAULT '', denied_ts REAL,
            detail_chars INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE pending_calls_v2 (owner TEXT, digest TEXT);
    """)
    digest = "c" * 64
    import time as _time
    now = _time.time()
    conn.execute(
        "INSERT INTO pending_calls (digest, tool, first_ts, last_ts) VALUES (?, ?, ?, ?)",
        (digest, "file_write", now, now),
    )
    conn.commit()
    conn.close()
    store = ApprovalLedger(path)
    assert [(item.digest, item.owner) for item in store.pending()] == [(digest, "")]
