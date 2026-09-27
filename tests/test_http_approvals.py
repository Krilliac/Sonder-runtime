"""S1 structured refusal receipts and S2 HTTP one-shot approvals.

The flow a phone drives: a chat ``/write`` in manual mode is refused
unattended, the response's ``sonder_receipt.refusal`` names the call, the
operator approves exactly that call once with ``POST /v1/approvals/<call_id>``,
and the retried, unchanged call runs once and spends the approval.
"""
from __future__ import annotations

import json
import time

import pytest

import permission_modes as pm
import server
import sonder_runtime.interfaces.http.serve as ts
from sonder_runtime.adapters.security.approval_ledger import ApprovalLedger
from sonder_runtime.interfaces.http.facades import approvals as facade
from tests.test_serve_auth import _http_server


def _request(port, method, path, body=None, headers=None):
    """Like test_serve_auth._request, with room for a cold catalog on a busy host."""
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()

pytestmark = pytest.mark.unit

API_KEY = "a" * 40
JSON = {"Content-Type": "application/json"}


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    store = ApprovalLedger(tmp_path / "approvals.db")
    monkeypatch.setattr(pm, "_approval_ledger", lambda: store)
    monkeypatch.setattr(pm, "_rule_lookup", lambda _tool: None)
    monkeypatch.setattr(pm, "_state_path", lambda: str(tmp_path / "mode.json"))
    saved, saved_loaded = dict(pm._STATE), pm._LOADED
    with pm._LOCK:
        pm._STATE.update(mode=pm.MANUAL, elevated=False, elevation_reason="")
    pm._LOADED = True
    pm.reset_unattended_for_tests()
    pm.risk_of("file_write")  # warm the command catalog outside any request timeout
    audits = []
    monkeypatch.setattr(server, "_record_direct_tool",
                        lambda *args, **kwargs: audits.append((args, kwargs)))
    store.audits = audits
    try:
        yield store
    finally:
        pm.forget_spent_approval()
        pm.reset_unattended_for_tests()
        with pm._LOCK:
            pm._STATE.clear()
            pm._STATE.update(saved)
        pm._LOADED = saved_loaded


def _local_open(monkeypatch):
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "local-open")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)


def _refuse_write(ledger, path="notes.txt", content="hello"):
    decision = pm.decide("file_write", interactive=False, surface="http",
                         arguments={"path": path, "content": content, "mode": "create"})
    assert decision.action == "deny" and decision.call_id
    return decision


# --- S1: the refusal receipt -------------------------------------------------

def test_refusal_receipt_names_the_call_and_every_route_out(ledger):
    decision = _refuse_write(ledger)
    receipt = facade.refusal_receipt(decision, mode_label="manual",
                                     modes_allowing=["acceptEdits", "auto"])
    assert receipt["kind"] == "refused"
    assert receipt["tool"] == "file_write"
    assert receipt["call_id"] == decision.call_id
    assert receipt["reason"] == decision.reason
    kinds = [item["kind"] for item in receipt["remedies"]]
    assert kinds == ["approve_once", "switch_mode", "allow_rule", "console"]
    assert receipt["remedies"][0]["path"] == "/v1/approvals/%s" % decision.call_id
    assert receipt["remedies"][1]["modes"] == ["acceptEdits", "auto"]
    # Arguments never appear.
    assert "hello" not in json.dumps(receipt)


def test_refusal_receipt_ignores_decisions_that_name_no_call(ledger):
    allowed = pm.decide("file_read", interactive=False, surface="http")
    unnamed = pm.decide("file_write", interactive=False, surface="http")
    assert facade.refusal_receipt(allowed) is None
    assert unnamed.action == "deny" and facade.refusal_receipt(unnamed) is None


def _chat(port, content, headers=None):
    body = json.dumps({"model": "sonder", "messages": [{"role": "user", "content": content}]})
    status, _, payload = _request(port, "POST", "/v1/chat/completions", body=body,
                                  headers={**JSON, **(headers or {})})
    return status, json.loads(payload)


def test_chat_write_refusal_carries_the_pending_call_id_and_approval_runs_it_once(ledger, monkeypatch):
    _local_open(monkeypatch)
    written = []
    monkeypatch.setattr(server, "file_write", lambda **kwargs: written.append(kwargs) or "wrote it")
    with _http_server(monkeypatch) as port:
        status, reply = _chat(port, "/write notes.txt hello")
        assert status == 200, reply
        text = reply["choices"][0]["message"]["content"]
        assert text.startswith("refused /write:")  # the text is unchanged
        refusal = reply["sonder_receipt"]["refusal"]
        assert refusal["kind"] == "refused" and refusal["tool"] == "file_write"
        assert refusal["mode"] == pm.MANUAL
        assert "/approve %s" % refusal["call_id"] in text
        assert written == []

        status, _, payload = _request(port, "GET", "/v1/approvals")
        assert status == 200
        pending = json.loads(payload)["pending"]
        assert [row["call_id"] for row in pending] == [refusal["call_id"]]
        assert pending[0]["tool"] == "file_write"

        status, _, payload = _request(port, "POST", "/v1/approvals/%s" % refusal["call_id"],
                                      body=json.dumps({"ttl_seconds": 600}), headers=JSON)
        assert status == 201, payload
        approval = json.loads(payload)["approval"]
        assert approval["approver"] == "local-open" and approval["surface"] == "http"
        assert approval["state"] == "open" and approval["ttl_seconds"] == 600

        # The unchanged call runs once and spends it ...
        status, reply = _chat(port, "/write notes.txt hello")
        assert reply["choices"][0]["message"]["content"] == "wrote it"
        assert "refusal" not in reply["sonder_receipt"]
        assert [{k: v for k, v in w.items() if k != "token"} for w in written] == [
            {"path": "notes.txt", "content": "hello", "mode": "create"}]
        # ... and only once.
        status, reply = _chat(port, "/write notes.txt hello")
        assert reply["choices"][0]["message"]["content"].startswith("refused /write:")
        assert len(written) == 1
        # A changed argument is a different call with a different id.
        status, other = _chat(port, "/write notes.txt HELLO")
        assert other["sonder_receipt"]["refusal"]["call_id"] != refusal["call_id"]
    assert any(args and args[0] == "permission_approve" for args, _ in ledger.audits)


# --- S2: the approval routes ------------------------------------------------

def _serve_with_accounts(monkeypatch, mode="account"):
    accounts = {
        "user-token": {"username": "ursula", "role": "user"},
        "dev-token": {"username": "dev", "role": "developer"},
        "admin-token": {"username": "ada", "role": "admin"},
    }
    monkeypatch.setattr(ts, "API_KEY", API_KEY if mode != "account" else "")
    monkeypatch.setattr(ts, "AUTH_MODE", mode)
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", mode == "account")
    monkeypatch.setattr(ts.Handler, "_auth_rate_limited", lambda self: False)
    monkeypatch.setattr(
        ts, "_auth_account",
        lambda header: accounts.get(str(header or "").replace("Bearer ", "", 1).strip()))


@pytest.mark.parametrize("token, expected", [
    (None, 401), ("user-token", 403), ("dev-token", 201), ("admin-token", 201),
])
def test_approve_authorization_matrix(ledger, monkeypatch, token, expected):
    _serve_with_accounts(monkeypatch)
    decision = _refuse_write(ledger)
    headers = dict(JSON)
    if token:
        headers["Authorization"] = "Bearer " + token
    with _http_server(monkeypatch) as port:
        status, _, payload = _request(port, "POST", "/v1/approvals/" + decision.call_id,
                                      body="{}", headers=headers)
        assert status == expected, payload
        listed, _, _ = _request(port, "GET", "/v1/approvals", headers=headers)
        assert listed == (200 if expected == 201 else expected)
    if expected == 201:
        approval = json.loads(payload)["approval"]
        username = {"dev-token": "dev", "admin-token": "ada"}[token]
        assert approval["approver"] == "developer:%s" % username
    elif expected == 403:
        body = json.loads(payload)["error"]
        assert body["code"] == "FORBIDDEN" and "developer" in body["message"]
        assert ledger.approvals() == []


def test_deployment_api_key_approves_as_admin_key(ledger, monkeypatch):
    _serve_with_accounts(monkeypatch, mode="api-key")
    decision = _refuse_write(ledger)
    with _http_server(monkeypatch) as port:
        status, _, payload = _request(
            port, "POST", "/v1/approvals/" + decision.call_id, body="{}",
            headers={**JSON, "Authorization": "Bearer " + API_KEY})
    assert status == 201, payload
    assert json.loads(payload)["approval"]["approver"] == "admin-key"


def test_idempotent_retry_does_not_issue_a_second_approval(ledger, monkeypatch):
    _local_open(monkeypatch)
    decision = _refuse_write(ledger)
    headers = {**JSON, "Idempotency-Key": "approve-once-1"}
    with _http_server(monkeypatch) as port:
        first = _request(port, "POST", "/v1/approvals/" + decision.call_id, body="{}", headers=headers)
        second = _request(port, "POST", "/v1/approvals/" + decision.call_id, body="{}", headers=headers)
        # Without a key a repeat is refused rather than stacking approvals.
        third = _request(port, "POST", "/v1/approvals/" + decision.call_id, body="{}", headers=JSON)
    assert first[0] == 201
    assert second[0] == 201 and json.loads(second[2]) == json.loads(first[2])
    assert third[0] == 409
    assert json.loads(third[2])["error"]["code"] == "APPROVAL_ALREADY_OPEN"
    assert len(ledger.approvals()) == 1


def test_single_spend_and_digest_binding(ledger):
    decision = _refuse_write(ledger)
    result = facade.approve_call(ledger, decision.call_id, {}, approver="developer:dev")
    assert result.status == 201
    # A changed argument does not match the approval ...
    changed = pm.decide("file_write", interactive=False, surface="http",
                        arguments={"path": "notes.txt", "content": "other", "mode": "create"})
    assert changed.action == "deny"
    # ... the exact call spends it once ...
    first = pm.decide("file_write", interactive=False, surface="http",
                      arguments={"path": "notes.txt", "content": "hello", "mode": "create"})
    assert first.action == "allow" and first.source == "approval"
    again = pm.decide("file_write", interactive=False, surface="http",
                      arguments={"path": "notes.txt", "content": "hello", "mode": "create"})
    assert again.action == "deny"


def test_body_digest_or_tool_that_disagrees_is_refused(ledger):
    decision = _refuse_write(ledger)
    wrong = facade.approve_call(ledger, decision.call_id, {"digest": "0" * 64}, approver="x")
    assert wrong.status == 409 and wrong.body["error"]["code"] == "CALL_DIGEST_MISMATCH"
    wrong_tool = facade.approve_call(ledger, decision.call_id, {"tool": "file_delete"}, approver="x")
    assert wrong_tool.status == 409
    digest = pm.call_digest("file_write", {"path": "notes.txt", "content": "hello", "mode": "create"})
    right = facade.approve_call(ledger, digest, {"digest": digest, "tool": "file_write"}, approver="x")
    assert right.status == 201 and right.body["approval"]["digest"] == digest
    assert ledger.approvals() and ledger.approvals()[0].digest == digest


@pytest.mark.parametrize("ref", ["", "abc", "3f9a12c0", "g" * 16, "0" * 17, "../etc"])
def test_call_ids_must_be_exact(ledger, ref):
    result = facade.approve_call(ledger, ref, {}, approver="x")
    assert result.status == 400 and result.body["error"]["code"] == "INVALID_CALL_ID"


def test_only_a_refused_pending_call_can_be_approved(ledger):
    result = facade.approve_call(ledger, "0123456789abcdef", {}, approver="x")
    assert result.status == 404 and result.body["error"]["code"] == "CALL_NOT_PENDING"


@pytest.mark.parametrize("body", [{"ttl_seconds": 5}, {"ttl_seconds": "900"}, {"ttl_seconds": True},
                                  {"ttl_seconds": 999_999}, {"extra": 1}, ["x"]])
def test_bad_bodies_are_refused(ledger, body):
    decision = _refuse_write(ledger)
    result = facade.approve_call(ledger, decision.call_id, body, approver="x")
    assert result.status == 400
    assert ledger.approvals() == []


def test_expiry(ledger, monkeypatch):
    decision = _refuse_write(ledger)
    assert facade.approve_call(ledger, decision.call_id, {"ttl_seconds": 60}, approver="x").status == 201
    later = time.time() + 61
    monkeypatch.setattr(time, "time", lambda: later)
    expired = pm.decide("file_write", interactive=False, surface="http",
                        arguments={"path": "notes.txt", "content": "hello", "mode": "create"})
    assert expired.action == "deny"
    listed = facade.approvals_payload(ledger, include_spent=True)
    assert listed["approvals"][0]["state"] == "expired"


def test_revoke_over_http(ledger, monkeypatch):
    _local_open(monkeypatch)
    decision = _refuse_write(ledger)
    with _http_server(monkeypatch) as port:
        _, _, payload = _request(port, "POST", "/v1/approvals/" + decision.call_id,
                                 body="{}", headers=JSON)
        nonce = json.loads(payload)["approval"]["nonce"]
        status, _, payload = _request(port, "POST", "/v1/approvals/revoke/" + nonce,
                                      body="{}", headers=JSON)
        assert status == 200, payload
        assert json.loads(payload)["approval"]["state"] == "revoked"
        status, _, payload = _request(port, "POST", "/v1/approvals/revoke/" + nonce,
                                      body="{}", headers=JSON)
        assert status == 404
        status, _, _ = _request(port, "POST", "/v1/approvals/revoke/not-a-nonce",
                                body="{}", headers=JSON)
        assert status == 400
    denied = pm.decide("file_write", interactive=False, surface="http",
                       arguments={"path": "notes.txt", "content": "hello", "mode": "create"})
    assert denied.action == "deny"


def test_ledger_unavailable_is_503(monkeypatch):
    _local_open(monkeypatch)
    monkeypatch.setattr(pm, "_approval_ledger", lambda: None)
    with _http_server(monkeypatch) as port:
        status, _, payload = _request(port, "GET", "/v1/approvals")
    assert status == 503
    assert json.loads(payload)["error"]["code"] == "APPROVALS_UNAVAILABLE"
