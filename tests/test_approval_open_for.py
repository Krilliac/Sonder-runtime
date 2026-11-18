"""Read-only ``open_for``, the deny route, and the run_code code preview.

A chat turn that waits inside its request for an operator to answer one
specific call polls ``ApprovalLedger.open_for`` (never spending anything),
learns of a refusal through ``denied_for``, and shows the operator the exact
code through the pending listing.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time

import permission_modes as pm
import pytest
import server
from sonder_runtime.adapters.security import approval_ledger as ledger_module
from sonder_runtime.adapters.security.approval_ledger import (
    DENIAL_TTL_SECONDS,
    MAX_DETAIL_CHARS,
    ApprovalDeniedError,
    ApprovalLedger,
)
from sonder_runtime.interfaces.http.facades import approvals as facade

from tests.test_http_approvals import (  # noqa: F401  (fixture)
    JSON,
    _http_server,
    _local_open,
    _refuse_write,
    _request,
    _serve_with_accounts,
    ledger,
)

pytestmark = pytest.mark.unit

DIGEST = "ab" * 32
OTHER = "cd" * 32


@pytest.fixture
def active(request):
    """The permission-gate ledger fixture from test_http_approvals, under a name
    this module's test arguments do not shadow the imported fixture with."""
    return request.getfixturevalue("ledger")


@pytest.fixture
def store(tmp_path):
    return ApprovalLedger(tmp_path / "approvals.db")


def _pend_and_issue(store, tool="run_code", digest=DIGEST, ttl=600):
    store.record_pending(tool, digest, surface="http", preview="code=<5 chars>")
    return store.issue(tool, digest, approver="dev", ttl_seconds=ttl)


# --- open_for ---------------------------------------------------------------

def test_open_for_is_true_only_for_the_exact_unspent_unexpired_approval(store):
    assert store.open_for("run_code", DIGEST) is False
    _pend_and_issue(store)
    assert store.open_for("run_code", DIGEST) is True
    assert store.open_for("/run_code", DIGEST.upper()) is True  # same normalisation as consume
    assert store.open_for("run_project", DIGEST) is False
    assert store.open_for("run_code", OTHER) is False
    assert store.open_for("run_code", DIGEST[:16]) is False
    assert store.open_for("", DIGEST) is False


def test_open_for_never_consumes_and_consume_still_works_after_many_polls(store):
    approval = _pend_and_issue(store)
    for _ in range(50):
        assert store.open_for("run_code", DIGEST) is True
    assert store.get(approval.nonce).state() == "open"
    spent = store.consume("run_code", DIGEST, surface="chat-tool")
    assert spent is not None and spent.nonce == approval.nonce
    assert store.open_for("run_code", DIGEST) is False
    assert store.consume("run_code", DIGEST) is None


def test_open_for_agrees_with_consume_on_spent_revoked_and_expired(store):
    approval = _pend_and_issue(store)
    assert store.revoke(approval.nonce) is not None
    assert store.open_for("run_code", DIGEST) is False
    assert store.consume("run_code", DIGEST) is None

    _pend_and_issue(store, digest=OTHER)
    with store._write() as conn:
        conn.execute("UPDATE approvals SET expires_ts=? WHERE digest=?", (time.time() - 1, OTHER))
    assert store.open_for("run_code", OTHER) is False
    assert store.consume("run_code", OTHER) is None


def test_open_for_and_denied_for_do_not_create_a_missing_database(tmp_path):
    path = tmp_path / "nested" / "a.db"
    missing = ApprovalLedger(path)
    assert missing.open_for("run_code", DIGEST) is False
    assert missing.denied_for("run_code", DIGEST) is False
    assert not path.exists() and not path.parent.exists()


def test_open_for_does_not_write(store):
    _pend_and_issue(store)
    before = store.approvals(include_spent=True)
    pending_before = store.pending()
    store.open_for("run_code", DIGEST)
    store.denied_for("run_code", DIGEST)
    assert store.approvals(include_spent=True) == before
    assert store.pending() == pending_before


# --- denial -----------------------------------------------------------------

def test_deny_marks_the_call_revokes_its_approval_and_blocks_consume(store):
    approval = _pend_and_issue(store)
    assert store.denied_for("run_code", DIGEST) is False
    denied = store.deny(DIGEST[:16])
    assert denied.denied and denied.digest == DIGEST
    assert store.denied_for("run_code", DIGEST) is True
    assert store.denied_for("run_code", OTHER) is False
    assert store.get(approval.nonce).revoked
    assert store.open_for("run_code", DIGEST) is False
    assert store.consume("run_code", DIGEST) is None


def _gate(store, args):
    return pm.decide("run_code", interactive=False, mode="manual", surface="chat-tool",
                     arguments=args, approval_ledger=store, record=True)


def test_issue_refuses_a_denied_call_and_the_retry_cannot_revive_anything(store):
    """Reviewer probe: deny, then a console issue, then the model's retries."""
    args = {"code": "print('x')"}
    first = _gate(store, args)
    assert first.action == "deny" and first.call_id
    digest = store.resolve_call(first.call_id).digest
    store.deny(first.call_id)
    with pytest.raises(ApprovalDeniedError):
        store.issue("run_code", digest, approver="console", ttl_seconds=600)
    assert store.approvals() == []
    assert _gate(store, args).action == "deny"      # refusal records pending again
    assert store.denied_for("run_code", digest) is True
    assert _gate(store, args).action == "deny"      # nothing was ever issued
    assert store.consume("run_code", digest) is None


def test_console_override_clears_the_denial_and_the_call_then_runs_once(store):
    args = {"code": "print('x')"}
    first = _gate(store, args)
    digest = store.resolve_call(first.call_id).digest
    store.deny(first.call_id)
    store.issue("run_code", digest, approver="console", ttl_seconds=600, override_denial=True)
    assert store.denied_for("run_code", digest) is False
    assert store.open_for("run_code", digest) is True
    assert _gate(store, args).action == "allow"
    assert store.consume("run_code", digest) is None   # spent exactly once


def test_a_concurrent_refusal_inside_the_ttl_leaves_the_denial_standing(store):
    store.record_pending("run_code", DIGEST, surface="chat-tool")   # turn A waiting
    store.deny(DIGEST)
    store.record_pending("run_code", DIGEST, surface="chat-tool")   # turn B / the model's retry
    assert store.denied_for("run_code", DIGEST) is True
    assert store.resolve_call(DIGEST).denied is True


def test_denial_lapses_after_the_ttl_and_a_fresh_refusal_clears_it(store, monkeypatch):
    store.record_pending("run_code", DIGEST, surface="chat-tool")
    store.deny(DIGEST)
    real = time.time
    monkeypatch.setattr(ledger_module.time, "time", lambda: real() + DENIAL_TTL_SECONDS + 5)
    assert store.denied_for("run_code", DIGEST) is False
    store.record_pending("run_code", DIGEST, surface="chat-tool")
    with store._read() as conn:
        assert conn.execute("SELECT denied_ts FROM pending_calls").fetchone()[0] is None
    store.issue("run_code", DIGEST, approver="dev", ttl_seconds=600)
    assert store.consume("run_code", DIGEST) is not None


def test_interleaved_approve_and_deny_never_leave_the_call_runnable(tmp_path):
    """Reviewer probe: a deny landing between approve's checks and its issue."""
    class Racing(ApprovalLedger):
        fired = False

        def approvals(self, **kw):
            if not Racing.fired:
                Racing.fired = True
                assert facade.deny_call(self, DIGEST[:16], {}).status == 200
            return super().approvals(**kw)

    racing = Racing(tmp_path / "a.db")
    racing.record_pending("run_code", DIGEST, surface="chat-tool")
    result = facade.approve_call(racing, DIGEST[:16], {}, approver="local-open")
    assert result.status == 409 and result.body["error"]["code"] == "CALL_DENIED"
    racing.record_pending("run_code", DIGEST, surface="chat-tool")   # the model retries
    assert racing.consume("run_code", DIGEST) is None
    assert racing.consume("run_code", DIGEST) is None
    assert racing.approvals() == []


def test_threaded_approve_and_deny_never_end_runnable(tmp_path):
    for round_no in range(8):
        store = ApprovalLedger(tmp_path / f"t{round_no}.db")
        store.record_pending("run_code", DIGEST, surface="chat-tool")
        barrier = threading.Barrier(2)
        outcome = {}

        def approve(barrier=barrier, outcome=outcome, store=store):
            barrier.wait()
            outcome["approve"] = facade.approve_call(store, DIGEST[:16], {}, approver="a").status

        def deny(barrier=barrier, outcome=outcome, store=store):
            barrier.wait()
            outcome["deny"] = facade.deny_call(store, DIGEST[:16], {}).status

        threads = [threading.Thread(target=approve), threading.Thread(target=deny)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        store.record_pending("run_code", DIGEST, surface="chat-tool")
        assert store.denied_for("run_code", DIGEST) is True
        assert store.open_for("run_code", DIGEST) is False
        assert store.consume("run_code", DIGEST) is None, outcome


def test_denial_does_not_block_another_digest(store):
    store.record_pending("run_code", DIGEST, surface="http")
    store.deny(DIGEST)
    _pend_and_issue(store, digest=OTHER)
    assert store.open_for("run_code", OTHER) is True
    assert store.consume("run_code", OTHER) is not None


def test_playbook_style_issue_cannot_approve_a_denied_call(store):
    store.record_pending("playbook_entry_approval", DIGEST, surface="playbook")
    store.deny(DIGEST)
    with pytest.raises(ApprovalDeniedError):
        store.issue("playbook_entry_approval", DIGEST, approver="owner", surface="playbook")


def test_console_approve_command_overrides_only_from_the_console(store, monkeypatch):
    monkeypatch.setattr(server.permission_modes, "approval_ledger", lambda: store)
    audits = []
    monkeypatch.setattr(server, "_record_direct_tool",
                        lambda *a, **k: audits.append((a, k)))
    store.record_pending("run_code", DIGEST, surface="chat-tool")
    store.deny(DIGEST)
    monkeypatch.setenv("SONDER_ALLOW_PERMISSION_EDITS", "1")
    tool_text = server.permission_approve(call_id=DIGEST[:16], token="")
    assert tool_text.startswith("ERROR") and "denied" in tool_text
    assert store.approvals() == []
    text = server.permission_approve(call_id=DIGEST[:16], token=server._OPERATOR_ATTENDED)
    assert text.startswith("overrode a denial: approved run_code"), text
    assert store.open_for("run_code", DIGEST) is True
    assert audits[-1][0][1].get("overrode_denial") is True


def test_deny_unknown_call_raises(store):
    with pytest.raises(ValueError):
        store.deny("ef" * 8)


def test_old_database_without_the_new_columns_is_migrated(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE pending_calls (digest TEXT PRIMARY KEY, tool TEXT NOT NULL, "
        "surface TEXT NOT NULL DEFAULT '', preview TEXT NOT NULL DEFAULT '', "
        "first_ts REAL NOT NULL, last_ts REAL NOT NULL, count INTEGER NOT NULL DEFAULT 1)")
    conn.execute("INSERT INTO pending_calls VALUES (?, 'run_code', 'http', 'p', ?, ?, 1)",
                 (DIGEST, time.time(), time.time()))
    conn.commit()
    conn.close()
    old = ApprovalLedger(path)
    pending = old.pending()
    assert [p.digest for p in pending] == [DIGEST] and pending[0].detail == ""
    assert old.deny(DIGEST).denied


# --- the code preview -------------------------------------------------------

def test_detail_is_bounded_with_a_truncation_marker_and_redacted(tmp_path):
    store = ApprovalLedger(tmp_path / "a.db", redact=lambda text: text.replace("SECRET", "***"))
    code = "x = 'SECRET'\n" + "y" * (MAX_DETAIL_CHARS * 2)
    pending = store.record_pending("run_code", DIGEST, detail=code)
    assert "SECRET" not in pending.detail and pending.detail.startswith("x = '***'")
    assert "truncated" in pending.detail and len(pending.detail) < MAX_DETAIL_CHARS + 100
    assert pending.detail_truncated and pending.detail_chars == len(code)
    assert f"of {len(code)} chars" in pending.detail
    short = store.record_pending("run_code", OTHER, detail="print(1)")
    assert short.detail == "print(1)" and not short.detail_truncated
    assert short.detail_chars == 8


def test_a_failing_redactor_stores_no_detail(tmp_path):
    def boom(_text):
        raise RuntimeError("x")
    store = ApprovalLedger(tmp_path / "a.db", redact=boom)
    assert store.record_pending("run_code", DIGEST, detail="print(1)").detail == ""


def test_run_code_pending_carries_the_code_and_the_digest_is_unchanged(active):
    code = "print('hello from the model')\n" * 3
    args = {"code": code}
    expected_digest = pm.call_digest("run_code", args)
    decision = pm.decide("run_code", interactive=False, surface="http", arguments=args)
    assert decision.action == "deny" and decision.call_id == expected_digest[:16]
    pending = active.resolve_call(decision.call_id)
    assert pending.digest == expected_digest
    assert pending.detail == code
    assert pending.preview == f"code=<{len(code)} chars>"  # the preview is unchanged
    assert pm.call_digest("run_code", args) == expected_digest
    assert pm.run_code_detail("file_write", {"code": "x"}) == ""
    assert pm.run_code_detail("run_code", {"code": 5}) == ""


def test_other_tools_get_no_detail(active):
    decision = _refuse_write(active)
    assert active.resolve_call(decision.call_id).detail == ""


def test_listing_hides_the_code_in_account_modes_for_every_account(active, monkeypatch):
    _serve_with_accounts(monkeypatch)
    code = "print(40 + 2)"
    decision = pm.decide("run_code", interactive=False, surface="http", arguments={"code": code})
    with _http_server(monkeypatch) as port:
        for token, expected in ((None, 401), ("user-token", 403), ("dev-token", 200),
                                ("admin-token", 200)):
            headers = {"Authorization": "Bearer " + token} if token else {}
            status, _, payload = _request(port, "GET", "/v1/approvals", headers=headers)
            assert status == expected, payload
            assert code not in payload.decode("utf-8", "replace")
            if expected == 200:
                row = json.loads(payload)["pending"][0]
                assert row["call_id"] == decision.call_id
                assert row["detail"] is None and row["detail_hidden"] is True
                assert row["denied"] is False


@pytest.mark.parametrize("mode", ["local-open", "api-key"])
def test_listing_shows_the_code_in_single_owner_modes(active, monkeypatch, mode):
    if mode == "local-open":
        _local_open(monkeypatch)
        headers = {}
    else:
        _serve_with_accounts(monkeypatch, mode="api-key")
        headers = {"Authorization": "Bearer a" * 1 + "a" * 39}
    code = "print(40 + 2)\n" + "z" * (MAX_DETAIL_CHARS + 50)
    pm.decide("run_code", interactive=False, surface="http", arguments={"code": code})
    with _http_server(monkeypatch) as port:
        status, _, payload = _request(port, "GET", "/v1/approvals", headers=headers)
    assert status == 200, payload
    row = json.loads(payload)["pending"][0]
    assert row["detail"].startswith("print(40 + 2)") and row["detail_hidden"] is False
    assert row["detail_truncated"] is True and row["detail_chars"] == len(code)


# --- the deny route ---------------------------------------------------------

@pytest.mark.parametrize("token", [None, "user-token", "dev-token", "admin-token"])
def test_deny_authorization_matches_approve(active, monkeypatch, token):
    """Deny needs exactly the authority approve needs, whatever that is in this tree."""
    _serve_with_accounts(monkeypatch)
    for_approve = _refuse_write(active, path="approve.txt")
    for_deny = _refuse_write(active, path="deny.txt")
    headers = dict(JSON)
    if token:
        headers["Authorization"] = "Bearer " + token
    with _http_server(monkeypatch) as port:
        approve_status, _, _ = _request(
            port, "POST", "/v1/approvals/" + for_approve.call_id, body="{}", headers=headers)
        status, _, payload = _request(
            port, "POST", "/v1/approvals/deny/" + for_deny.call_id, body="{}", headers=headers)
    allowed = approve_status == 201
    assert status == (200 if allowed else approve_status), payload
    digest = active.resolve_call(for_deny.call_id).digest
    assert active.denied_for("file_write", digest) is allowed


def test_deny_route_blocks_consume_and_is_idempotent(active, monkeypatch):
    _local_open(monkeypatch)
    decision = _refuse_write(active)
    digest = active.resolve_call(decision.call_id).digest
    headers = {**JSON, "Idempotency-Key": "deny-1"}
    with _http_server(monkeypatch) as port:
        first = _request(port, "POST", "/v1/approvals/deny/" + decision.call_id,
                         body="{}", headers=headers)
        second = _request(port, "POST", "/v1/approvals/deny/" + decision.call_id,
                          body="{}", headers=headers)
        assert first[0] == 200, first[2]
        assert second[0] == 200 and json.loads(second[2]) == json.loads(first[2])
        assert json.loads(first[2])["denial"]["call_id"] == decision.call_id
        # An approval after a denial is refused, so nothing can be spent.
        late = _request(port, "POST", "/v1/approvals/" + decision.call_id, body="{}", headers=JSON)
        assert late[0] == 409 and json.loads(late[2])["error"]["code"] == "CALL_DENIED"
        # Unknown call, bad id, disagreeing digest.
        assert _request(port, "POST", "/v1/approvals/deny/" + "ef" * 8,
                        body="{}", headers=JSON)[0] == 404
        assert _request(port, "POST", "/v1/approvals/deny/xyz", body="{}", headers=JSON)[0] == 400
        assert _request(port, "POST", "/v1/approvals/deny/" + decision.call_id,
                        body=json.dumps({"digest": "0" * 64}), headers=JSON)[0] == 409
    assert active.approvals() == []
    assert active.consume("file_write", digest) is None
    deny_audits = [(a, k) for a, k in active.audits
                   if a and a[0] == "permission_approve" and "deny" in a[1]]
    assert deny_audits, active.audits
    args, kwargs = deny_audits[0]
    assert args[1] == {"deny": decision.call_id, "tool": "file_write", "actor": "local-open"}
    assert kwargs["summary"] == f"denied file_write call {decision.call_id} via http by local-open"


def test_deny_route_revokes_an_open_approval_and_leaves_other_calls(active, monkeypatch):
    _local_open(monkeypatch)
    first = _refuse_write(active, path="a.txt")
    other = _refuse_write(active, path="b.txt")
    with _http_server(monkeypatch) as port:
        status, _, payload = _request(port, "POST", "/v1/approvals/" + first.call_id,
                                      body="{}", headers=JSON)
        assert status == 201, payload
        assert _request(port, "POST", "/v1/approvals/deny/" + first.call_id,
                        body="{}", headers=JSON)[0] == 200
        assert _request(port, "POST", "/v1/approvals/" + other.call_id,
                        body="{}", headers=JSON)[0] == 201
    first_digest = active.resolve_call(first.call_id).digest
    other_digest = active.resolve_call(other.call_id).digest
    assert active.open_for("file_write", first_digest) is False
    assert active.open_for("file_write", other_digest) is True
    assert active.consume("file_write", first_digest) is None
    assert active.consume("file_write", other_digest) is not None
