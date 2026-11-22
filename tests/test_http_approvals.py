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


def test_local_open_refusal_receipt_offers_no_http_grant(ledger):
    """Neither the approve POST nor a mode raise works without a credential."""
    from sonder_runtime.interfaces.http.facades import approval_routes, permission_mode_post
    from sonder_runtime.interfaces.http.facades.local_open_authority import (
        AUTHENTICATED_ADMIN_REMEDY)

    decision = _refuse_write(ledger)
    receipt = facade.refusal_receipt(
        decision, mode_label="manual", modes_allowing=["acceptEdits", "auto"],
        credential_less=True, raises=["acceptEdits", "auto"])
    call = decision.call_id
    assert receipt["call_id"] == call
    assert receipt["remedies"] == [
        {"kind": "approve_once", "call_id": call, "console": "/approve %s" % call,
         "http_available": False, "http_refusal": approval_routes.GRANT_REFUSED_CODE},
        {"kind": "switch_mode", "modes": ["acceptEdits", "auto"],
         "console": "/mode acceptEdits", "http_available": False,
         "http_refusal": permission_mode_post.ESCALATION_CODE},
        {"kind": "allow_rule", "console": "/permissions"},
        {"kind": "console", "detail": "run it from the console and answer the prompt"},
        {"kind": "authenticate", "detail": AUTHENTICATED_ADMIN_REMEDY},
    ]
    # No remedy points at a route that would answer 403.
    assert "/v1/approvals" not in json.dumps(receipt["remedies"])
    assert not any("method" in item or "path" in item for item in receipt["remedies"])


def test_local_open_refusal_receipt_keeps_a_mode_change_that_is_not_a_raise(ledger):
    decision = _refuse_write(ledger)
    receipt = facade.refusal_receipt(
        decision, modes_allowing=["acceptEdits", "auto"],
        credential_less=True, raises=["auto"])
    switches = [item for item in receipt["remedies"] if item["kind"] == "switch_mode"]
    assert switches == [
        {"kind": "switch_mode", "modes": ["acceptEdits"]},
        {"kind": "switch_mode", "modes": ["auto"], "console": "/mode auto",
         "http_available": False, "http_refusal": "PERMISSION_MODE_ESCALATION_REFUSED"},
    ]


def test_turn_receipt_counts_only_a_real_raise_as_refused(monkeypatch):
    """The ``raises`` the turn receipt computes match the POST's own rule."""
    from sonder_runtime.adapters.security.permission_policy import permission_policy

    def raising(current, modes):
        return facade.modes_raising_from(permission_policy, current, modes)

    assert raising(pm.MANUAL, [pm.ACCEPT_EDITS, pm.AUTO]) == [pm.ACCEPT_EDITS, pm.AUTO]
    assert raising(pm.AUTO, [pm.ACCEPT_EDITS]) == []
    assert raising(pm.PLAN, [pm.MANUAL, pm.AUTO]) == [pm.AUTO]

    def broken(current, target):
        raise RuntimeError("policy unavailable")

    monkeypatch.setattr(pm, "is_unattended_escalation", broken)
    # Fails closed: when the policy cannot say, every mode is a raise.
    assert raising(pm.AUTO, [pm.ACCEPT_EDITS]) == [pm.ACCEPT_EDITS]


def test_chat_refusal_receipt_follows_the_auth_mode(ledger, monkeypatch):
    """Over the wire: local-open marks the grants unavailable; an API key keeps them."""
    monkeypatch.setattr(server, "file_write", lambda **kwargs: "wrote it")
    _local_open(monkeypatch)
    with _http_server(monkeypatch) as port:
        status, reply = _chat(port, "/write notes.txt hello")
        assert status == 200, reply
        refusal = reply["sonder_receipt"]["refusal"]
    by_kind = {}
    for item in refusal["remedies"]:
        by_kind.setdefault(item["kind"], []).append(item)
    (approve,) = by_kind["approve_once"]
    assert approve == {
        "kind": "approve_once", "call_id": refusal["call_id"],
        "console": "/approve %s" % refusal["call_id"],
        "http_available": False, "http_refusal": GRANT_REFUSED}
    # From manual every mode that allows a file change is a raise.
    assert by_kind["switch_mode"] == [
        {"kind": "switch_mode", "modes": [pm.ACCEPT_EDITS, pm.AUTO],
         "console": "/mode %s" % pm.ACCEPT_EDITS, "http_available": False,
         "http_refusal": "PERMISSION_MODE_ESCALATION_REFUSED"}]
    (authenticate,) = by_kind["authenticate"]
    assert "SONDER_API_KEY" in authenticate["detail"]
    assert "SONDER_REQUIRE_ACCOUNT=1" in authenticate["detail"]

    _serve_with_accounts(monkeypatch, mode="api-key")
    with _http_server(monkeypatch) as port:
        status, reply = _chat(port, "/write notes.txt hello",
                              {"Authorization": "Bearer " + API_KEY})
        assert status == 200, reply
        refusal = reply["sonder_receipt"]["refusal"]
    assert refusal["remedies"][:2] == [
        {"kind": "approve_once", "call_id": refusal["call_id"], "method": "POST",
         "path": "/v1/approvals/%s" % refusal["call_id"],
         "console": "/approve %s" % refusal["call_id"]},
        {"kind": "switch_mode", "modes": [pm.ACCEPT_EDITS, pm.AUTO]},
    ]
    assert [item["kind"] for item in refusal["remedies"]] == [
        "approve_once", "switch_mode", "allow_rule", "console"]


def test_refusal_receipt_ignores_decisions_that_name_no_call(ledger):
    allowed = pm.decide("file_read", interactive=False, surface="http")
    unnamed = pm.decide("file_write", interactive=False, surface="http")
    assert facade.refusal_receipt(allowed) is None
    assert unnamed.action == "deny" and facade.refusal_receipt(unnamed) is None


KEY = {"Authorization": "Bearer " + API_KEY}


def _owner_key(monkeypatch):
    _serve_with_accounts(monkeypatch, mode="api-key")


def _chat(port, content, headers=None):
    body = json.dumps({"model": "sonder", "messages": [{"role": "user", "content": content}]})
    status, _, payload = _request(port, "POST", "/v1/chat/completions", body=body,
                                  headers={**JSON, **(headers or {})})
    return status, json.loads(payload)


def test_chat_write_refusal_carries_the_pending_call_id_and_approval_runs_it_once(ledger, monkeypatch):
    # Was driven under local-open, which may no longer approve (see
    # test_local_open_cannot_approve_a_refused_call); the end-to-end flow is
    # the owner API key's.
    _owner_key(monkeypatch)
    written = []
    monkeypatch.setattr(server, "file_write", lambda **kwargs: written.append(kwargs) or "wrote it")
    with _http_server(monkeypatch) as port:
        status, reply = _chat(port, "/write notes.txt hello", KEY)
        assert status == 200, reply
        text = reply["choices"][0]["message"]["content"]
        assert text.startswith("refused /write:")  # the text is unchanged
        refusal = reply["sonder_receipt"]["refusal"]
        assert refusal["kind"] == "refused" and refusal["tool"] == "file_write"
        assert refusal["mode"] == pm.MANUAL
        assert "/approve %s" % refusal["call_id"] in text
        assert written == []

        status, _, payload = _request(port, "GET", "/v1/approvals", headers=KEY)
        assert status == 200
        pending = json.loads(payload)["pending"]
        assert [row["call_id"] for row in pending] == [refusal["call_id"]]
        assert pending[0]["tool"] == "file_write"

        status, _, payload = _request(port, "POST", "/v1/approvals/%s" % refusal["call_id"],
                                      body=json.dumps({"ttl_seconds": 600}), headers={**JSON, **KEY})
        assert status == 201, payload
        approval = json.loads(payload)["approval"]
        assert approval["approver"] == "admin-key" and approval["surface"] == "http"
        assert approval["state"] == "open" and approval["ttl_seconds"] == 600

        # The unchanged call runs once and spends it ...
        status, reply = _chat(port, "/write notes.txt hello", KEY)
        assert reply["choices"][0]["message"]["content"] == "wrote it"
        assert "refusal" not in reply["sonder_receipt"]
        assert [{k: v for k, v in w.items() if k != "token"} for w in written] == [
            {"path": "notes.txt", "content": "hello", "mode": "create"}]
        # ... and only once.
        status, reply = _chat(port, "/write notes.txt hello", KEY)
        assert reply["choices"][0]["message"]["content"].startswith("refused /write:")
        assert len(written) == 1
        # A changed argument is a different call with a different id.
        status, other = _chat(port, "/write notes.txt HELLO", KEY)
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


# Approving is the authority chat's ``/permission_approve`` needs: an
# administrator (``SYSTEM_OPERATION_TOOLS["permission_approve"]`` is an
# admin-bound operation). Reviewing the list stays developer-readable.
@pytest.mark.parametrize("token, expected, listed_expected, refusal", [
    (None, 401, 401, ""),
    ("user-token", 403, 403, "developer"),
    ("dev-token", 403, 200, "administrator"),
    ("admin-token", 201, 200, ""),
])
def test_approve_authorization_matrix(ledger, monkeypatch, token, expected,
                                      listed_expected, refusal):
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
        assert listed == listed_expected
    if expected == 201:
        approval = json.loads(payload)["approval"]
        assert approval["approver"] == "admin:ada"
    elif expected == 403:
        body = json.loads(payload)["error"]
        assert body["code"] == "FORBIDDEN" and refusal in body["message"]
        assert ledger.approvals() == []


def test_approve_authority_matches_chat_permission_approve(ledger, monkeypatch):
    """The route and ``/permission_approve`` in chat refuse the same callers."""
    _serve_with_accounts(monkeypatch)
    for role in ("user", "developer", "admin"):
        context = {"authorized": True, "mode": "account", "api_key": False,
                   "account": {"username": role, "role": role}}
        chat = ts._system_operation_authority_error(
            ts._http_system_operation_for("permission_approve"), context)
        route = ts.Handler._approval_ports(None).approve_authority_error(context)
        assert route == chat, role
        assert bool(route) is (role != "admin"), role


def test_http_approver_names_the_accounts_real_role():
    """The ledger's approver label names the role that authorized the call."""
    class _Account:
        username, role = "obi", "admin"

    def label(account, **extra):
        return ts._http_approver({"mode": "account", "account": account, **extra})

    assert label({"username": "ada", "role": "admin"}) == "admin:ada"
    assert label({"username": "dev", "role": "developer"}) == "developer:dev"
    assert label(_Account()) == "admin:obi"
    assert label({"username": "", "role": ""}) == "account:account"
    assert ts._http_approver({"mode": "local-open"}) == "local-open"
    assert ts._http_approver({"mode": "api-key", "api_key": True}) == "admin-key"


def test_developer_cannot_revoke_an_approval(ledger, monkeypatch):
    _serve_with_accounts(monkeypatch)
    decision = _refuse_write(ledger)
    admin = {**JSON, "Authorization": "Bearer admin-token"}
    with _http_server(monkeypatch) as port:
        _, _, payload = _request(port, "POST", "/v1/approvals/" + decision.call_id,
                                 body="{}", headers=admin)
        nonce = json.loads(payload)["approval"]["nonce"]
        status, _, payload = _request(port, "POST", "/v1/approvals/revoke/" + nonce, body="{}",
                                      headers={**JSON, "Authorization": "Bearer dev-token"})
    assert status == 403, payload
    assert "administrator" in json.loads(payload)["error"]["message"]
    assert ledger.approvals()[0].state() == "open"


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
    # Was local-open; a local-open approval is now refused before any receipt,
    # so replay is shown for the authenticated caller that can approve.
    _owner_key(monkeypatch)
    decision = _refuse_write(ledger)
    headers = {**JSON, **KEY, "Idempotency-Key": "approve-once-1"}
    with _http_server(monkeypatch) as port:
        first = _request(port, "POST", "/v1/approvals/" + decision.call_id, body="{}", headers=headers)
        second = _request(port, "POST", "/v1/approvals/" + decision.call_id, body="{}", headers=headers)
        # Without a key a repeat is refused rather than stacking approvals.
        third = _request(port, "POST", "/v1/approvals/" + decision.call_id, body="{}",
                         headers={**JSON, **KEY})
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
    # Revoking stays allowed under local-open; the approval it withdraws used
    # to be issued by a local-open POST, which is now refused, so it is issued
    # off-HTTP here, as an attended approval would be.
    _local_open(monkeypatch)
    decision = _refuse_write(ledger)
    issued = facade.approve_call(ledger, decision.call_id, {}, approver="console", surface="console")
    assert issued.status == 201
    nonce = issued.body["approval"]["nonce"]
    with _http_server(monkeypatch) as port:
        status, _, payload = _request(port, "POST", "/v1/approvals/revoke/" + nonce,
                                      body="{}", headers=JSON)
        assert status == 200, payload
        assert json.loads(payload)["approval"]["state"] == "revoked"
        status, _, payload = _request(port, "POST", "/v1/approvals/revoke/" + nonce,
                                      body="{}", headers={**JSON, **KEY})
        assert status == 404
        status, _, _ = _request(port, "POST", "/v1/approvals/revoke/not-a-nonce",
                                body="{}", headers={**JSON, **KEY})
        assert status == 400
    denied = pm.decide("file_write", interactive=False, surface="http",
                       arguments={"path": "notes.txt", "content": "hello", "mode": "create"})
    assert denied.action == "deny"


def test_an_encoded_reference_is_decoded_once_and_a_double_encoded_one_is_refused(ledger, monkeypatch):
    _owner_key(monkeypatch)
    decision = _refuse_write(ledger)
    headers = {**JSON, **KEY}
    with _http_server(monkeypatch) as port:
        for raw_ref in (decision.call_id + "%2540-", decision.call_id + "%40%ff", decision.call_id + "%4"):
            status, _, payload = _request(port, "POST", "/v1/approvals/" + raw_ref,
                                          body="{}", headers=headers)
            assert status == 400, (raw_ref, payload)
        assert ledger.approvals() == []
        # A client that percent-encodes '@' names the same row as a raw '@'.
        status, _, payload = _request(port, "POST", "/v1/approvals/%s%%40-" % decision.call_id,
                                      body="{}", headers=headers)
        assert status == 201, payload
        nonce = json.loads(payload)["approval"]["nonce"]
        status, _, payload = _request(port, "POST", "/v1/approvals/revoke/" + nonce.replace("_", "%5F"),
                                      body="{}", headers=headers)
        assert status == 200, payload
        assert json.loads(payload)["approval"]["state"] == "revoked"


def test_ledger_unavailable_is_503(monkeypatch):
    _local_open(monkeypatch)
    monkeypatch.setattr(pm, "_approval_ledger", lambda: None)
    with _http_server(monkeypatch) as port:
        status, _, payload = _request(port, "GET", "/v1/approvals")
    assert status == 503
    assert json.loads(payload)["error"]["code"] == "APPROVALS_UNAVAILABLE"


# --- local-open may not approve: no credential stands behind the grant -------

GRANT_REFUSED = "APPROVAL_REQUIRES_AUTHENTICATED_ADMIN"


def _assert_grant_refused(status, payload, call_id):
    assert status == 403, payload
    error = json.loads(payload)["error"]
    assert error["code"] == GRANT_REFUSED
    assert error["retryable"] is False
    message = error["message"]
    assert "local-open" in message
    assert "SONDER_API_KEY" in message and "SONDER_REQUIRE_ACCOUNT=1" in message
    assert "/approve %s" % call_id in message


@pytest.mark.parametrize("headers", [
    {},
    # An admin account token does not help: the effective mode stays local-open.
    {"Authorization": "Bearer admin-token"},
], ids=["no-credential", "admin-account-token"])
def test_local_open_cannot_approve_a_refused_call(ledger, monkeypatch, headers):
    _local_open(monkeypatch)
    monkeypatch.setattr(
        ts, "_auth_account",
        lambda header: {"username": "ada", "role": "admin"}
        if "admin-token" in str(header or "") else None)
    written = []
    monkeypatch.setattr(server, "file_write", lambda **kwargs: written.append(kwargs) or "wrote it")
    with _http_server(monkeypatch) as port:
        status, reply = _chat(port, "/write notes.txt hello", headers)
        assert status == 200, reply
        call_id = reply["sonder_receipt"]["refusal"]["call_id"]
        for extra in ({}, {"Idempotency-Key": "grant-1"}, {"Idempotency-Key": "grant-1"}):
            status, _, payload = _request(port, "POST", "/v1/approvals/" + call_id,
                                          body="{}", headers={**JSON, **headers, **extra})
            _assert_grant_refused(status, payload, call_id)
        # Listing stays allowed, and the call is still pending, not approved.
        status, _, payload = _request(port, "GET", "/v1/approvals", headers=headers)
        assert status == 200
        listed = json.loads(payload)
        assert [row["call_id"] for row in listed["pending"]] == [call_id]
        assert listed["approvals"] == []
        status, reply = _chat(port, "/write notes.txt hello", headers)
        assert reply["choices"][0]["message"]["content"].startswith("refused /write:")
    assert written == []
    assert ledger.approvals(include_spent=True) == []
    assert not any(args and args[0] == "permission_approve" for args, _ in ledger.audits)


def test_local_open_approval_cannot_unlock_chat_run_code(ledger, monkeypatch):
    """The confirmed chain: approve the /run_code refusal's call id, re-run it."""
    from sonder_runtime.adapters.execution_tools import code_runner

    _local_open(monkeypatch)
    ran = []

    def runner(*args, **kwargs):
        ran.append((args, kwargs))
        return {"ok": True, "returncode": 0, "stdout": "42", "stderr": "",
                "language": "python"}

    monkeypatch.setattr(code_runner, "run_code", runner)
    with _http_server(monkeypatch) as port:
        status, reply = _chat(port, "/run_code print(6*7)")
        assert status == 200, reply
        call_id = reply["sonder_receipt"]["refusal"]["call_id"]
        status, _, payload = _request(port, "POST", "/v1/approvals/" + call_id,
                                      body="{}", headers=JSON)
        status_again, again = _chat(port, "/run_code print(6*7)")
    # Checked first so the pre-fix failure names the harm, not the status.
    assert ran == [], "host code ran after a credential-less approval: %r" % again
    _assert_grant_refused(status, payload, call_id)
    assert again["choices"][0]["message"]["content"].startswith("refused /run_code:")
    assert ledger.approvals(include_spent=True) == []


def test_unknown_or_missing_auth_context_cannot_approve(ledger):
    """``serve_post`` fails closed when it is handed no usable auth context."""
    from types import SimpleNamespace

    from sonder_runtime.interfaces.http.facades import approval_routes

    def recording_handler(sent):
        return SimpleNamespace(
            headers={}, _correlation=lambda: "corr",
            _send_json_payload=lambda payload, status=200: sent.append((payload, status)),
            _send_auth_error=lambda: sent.append(("auth", 401)),
            _send_not_found=lambda: sent.append(("missing", 404)),
        )

    decision = _refuse_write(ledger)
    for context in (None, {}, {"mode": "", "authorized": True}, {"mode": "open", "authorized": True}):
        sent = []
        handler = recording_handler(sent)
        ports = approval_routes.HttpApprovalPorts(
            developer_authorized=lambda _c: True,
            error_envelope=lambda code, message, corr, retryable=False: {
                "error": {"code": code, "message": message, "retryable": retryable}},
            ledger=lambda: ledger,
            idempotent_action=lambda *_a: pytest.fail("a refused grant ran its action"),
            send_idempotency_refusal=lambda _h, _r: False,
            approver_of=lambda _c: "x", audit=lambda *_a: None,
            clock=time.time, log=SimpleNamespace(warning=lambda *a, **k: None),
        )
        approval_routes.serve_post(handler, "/v1/approvals/" + decision.call_id, {}, context, ports)
        assert len(sent) == 1, (context, sent)
        payload, status = sent[0]
        assert status == 403 and payload["error"]["code"] == GRANT_REFUSED, (context, sent)
    assert ledger.approvals(include_spent=True) == []


def test_refused_grant_does_not_echo_a_malformed_path(ledger, monkeypatch):
    _local_open(monkeypatch)
    with _http_server(monkeypatch) as port:
        status, _, payload = _request(port, "POST", "/v1/approvals/%3Cscript%3E",
                                      body="{}", headers=JSON)
    assert status == 403, payload
    message = json.loads(payload)["error"]["message"]
    assert "/approve <call_id>" in message and "script" not in message


def test_authenticated_developer_and_admin_keep_approving(ledger, monkeypatch):
    """account mode with a developer or admin account, and the API key: unchanged."""
    _serve_with_accounts(monkeypatch, mode="account")
    first = _refuse_write(ledger, path="a.txt")
    with _http_server(monkeypatch) as port:
        status, _, payload = _request(port, "POST", "/v1/approvals/" + first.call_id, body="{}",
                                      headers={**JSON, "Authorization": "Bearer admin-token"})
    assert status == 201, payload
    _serve_with_accounts(monkeypatch, mode="both")
    second = _refuse_write(ledger, path="b.txt")
    with _http_server(monkeypatch) as port:
        status, _, payload = _request(
            port, "POST", "/v1/approvals/" + second.call_id, body="{}",
            headers={**JSON, "Authorization": "Bearer " + API_KEY,
                     "X-Sonder-Account-Token": "admin-token"})
    assert status == 201, payload
