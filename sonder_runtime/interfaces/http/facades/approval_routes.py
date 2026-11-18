"""HTTP approval routes: list refused calls, approve one once, revoke.

* ``GET /v1/approvals`` -- refused calls waiting for approval, and approvals;
* ``POST /v1/approvals/<call_id>`` -- approve one refused call once;
* ``POST /v1/approvals/revoke/<nonce>`` -- withdraw an open approval;
* ``POST /v1/approvals/deny/<call_id>`` -- decline one refused call (authorises
  nothing; revokes its open approval and lets a waiting turn see the refusal).

Reviewing the list needs a developer or administrator. Approving or revoking
issues authority another caller spends later, so it needs exactly what the
``permission_approve`` tool needs on the chat ``/<tool>`` path: the
administrator role its system operation is bound to (the single-user
local-open listener and the deployment API key count, exactly as for
``/v1/permission-mode``). The ledger semantics live in ``approvals.py``.

``handler`` is serve.py's request handler: these functions use its auth
check, its correlation id, its headers and its JSON sender, so the wire
behaviour is the handler's. Everything process-bound -- the authority check,
the error envelope, the approval ledger, the replay guard, the approver
label, the audit record and the logger -- arrives as
:class:`HttpApprovalPorts` from ``serve.py``.
"""
from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, Callable

from .approvals import approvals_payload, approve_call, deny_call, revoke_approval

ROUTE = "/v1/approvals"
_SINGLE_OWNER_MODES = frozenset({"local-open", "api-key"})


def _unavailable() -> dict:
    return {"error": {"message": "one-shot approvals are not available",
                      "type": "server_error", "code": "APPROVALS_UNAVAILABLE"}}


@dataclass(frozen=True)
class HttpApprovalPorts:
    developer_authorized: Callable[[Any], bool]
    # "" when the caller may approve or revoke, else the refusal reason.
    approve_authority_error: Callable[[Any], str]
    error_envelope: Callable[..., Any]
    ledger: Callable[[], Any]
    idempotent_action: Callable[..., Any]
    send_idempotency_refusal: Callable[[Any, Any], bool]
    approver_of: Callable[[Any], str]
    audit: Callable[[str, Any, float], None]
    clock: Callable[[], float]
    log: Any


def _forbidden(handler: Any, ports: HttpApprovalPorts, message: str) -> None:
    handler._send_json_payload(
        ports.error_envelope("FORBIDDEN", message, handler._correlation(), retryable=False),
        status=403,
    )


def _access(handler: Any, context: Any, ports: HttpApprovalPorts, *,
            issuing: bool = False) -> tuple[Any, bool]:
    """(ledger, done): ``done`` when an error response was already sent.

    ``issuing`` (approve or revoke) adds the ``permission_approve`` authority
    on top of the developer check that reviewing needs.
    """
    if not context["authorized"]:
        handler._send_auth_error()
        return None, True
    if not ports.developer_authorized(context):
        _forbidden(handler, ports,
                   "developer or administrator authorization is required "
                   "to review or approve calls")
        return None, True
    if issuing:
        authority_error = ports.approve_authority_error(context)
        if authority_error:
            _forbidden(handler, ports,
                       "%s to approve or revoke calls" % authority_error)
            return None, True
    try:
        ledger = ports.ledger()
    except Exception:
        ports.log.error("approval ledger unavailable", exc_info=True)
        ledger = None
    if ledger is None:
        handler._send_json_payload(
            {"error": {"message": "one-shot approvals are not available in this process",
                       "type": "server_error", "code": "APPROVALS_UNAVAILABLE"}},
            status=503,
        )
        return None, True
    return ledger, False


def _approval_owner_for_context(context: Any) -> str:
    account = (context or {}).get("account") or {}
    username = str(account.get("username") or account.get("id") or "").strip()
    if not username:
        return ""
    principal = "account:%s" % username
    return "ap-" + hashlib.sha256(principal.encode("utf-8")).hexdigest()


def _approval_include_unowned(context: Any) -> bool:
    account = (context or {}).get("account") or {}
    role = str(account.get("role") or "").strip().lower()
    return role in ("developer", "admin") or (context or {}).get("mode") in _SINGLE_OWNER_MODES


def serve_get(handler: Any, path: str, parse_query: Callable[[], dict],
              ports: HttpApprovalPorts) -> bool:
    """``GET /v1/approvals``; ``False`` for another route."""
    if (path.rstrip("/") or "/") != ROUTE:
        return False
    context = handler._request_auth_context()
    ledger, done = _access(handler, context, ports)
    if done:
        return True
    query = parse_query()
    try:
        limit = int((query.get("limit") or ["20"])[0])
    except ValueError:
        limit = 0
    if not 1 <= limit <= 200:
        handler._send_json_payload(
            {"error": {"message": "limit must be between 1 and 200",
                       "type": "invalid_request"}}, status=400)
        return True
    include_spent = (query.get("include_spent") or [""])[0].lower() in ("1", "true", "yes")
    try:
        # Scope ledger queries to the authenticated account's owner key.
        scope_fn = getattr(ledger, "scope", None)
        scope_ctx = (
            scope_fn(
                _approval_owner_for_context(context),
                include_unowned=_approval_include_unowned(context),
                # An administrator (the local-open listener and the deployment
                # key count) is the operator: it sees every owner's calls.
                all_owners=not ports.approve_authority_error(context),
            )
            if callable(scope_fn)
            else nullcontext()
        )
        with scope_ctx:
            payload = approvals_payload(
                ledger, limit=limit, include_spent=include_spent,
                show_detail=context.get("mode") in _SINGLE_OWNER_MODES,
            )
    except Exception:
        ports.log.error("approval ledger read failed", exc_info=True)
        handler._send_json_payload(_unavailable(), status=503)
        return True
    handler._send_json_payload(payload)
    return True


def serve_post(handler: Any, path: str, req: Any, context: Any,
               ports: HttpApprovalPorts) -> None:
    """``POST /v1/approvals/<call_id>`` and ``POST /v1/approvals/revoke/<nonce>``.

    The approval is bound to that call's digest, single-use and expiring,
    recorded as the caller's approver label with surface ``http``, and
    audited on the direct-tool path as ``permission_approve``.
    """
    parts = path[len(ROUTE):].strip("/").split("/")
    if len(parts) == 2 and parts[0] == "revoke" and parts[1]:
        action, target = "revoke", parts[1]
    elif len(parts) == 2 and parts[0] == "deny" and parts[1]:
        action, target = "deny", parts[1]
    elif len(parts) == 1 and parts[0] and parts[0] not in ("revoke", "deny"):
        action, target = "approve", parts[0]
    else:
        handler._send_not_found()
        return
    ledger, done = _access(handler, context, ports, issuing=True)
    if done:
        return
    approver = ports.approver_of(context)
    started = ports.clock()
    owner_key = _approval_owner_for_context(context)
    include_unowned = _approval_include_unowned(context)

    def run():
        scope_fn = getattr(ledger, "scope", None)
        scope_ctx = (
            # Every caller here passed the administrator check in _access
            # (issuing=True), so it approves, denies and revokes any owner's call.
            scope_fn(owner_key, include_unowned=include_unowned, all_owners=True)
            if callable(scope_fn)
            else nullcontext()
        )
        with scope_ctx:
            if action == "revoke":
                return revoke_approval(ledger, target)
            if action == "deny":
                return deny_call(ledger, target, req)
            return approve_call(ledger, target, req, approver=approver, surface="http")

    action_text = "approval\0%s\0%s\0%s" % (
        action, target.lower(), json.dumps(req, sort_keys=True, default=str),
    )
    try:
        result = ports.idempotent_action(
            context, handler.headers.get("Idempotency-Key", ""), action_text, run,
        )
    except Exception:
        ports.log.error("approval request failed", exc_info=True)
        handler._send_json_payload(_unavailable(), status=503)
        return
    if ports.send_idempotency_refusal(handler, result):
        return
    if result.status < 300:
        body = result.body or {}
        approval = {**(body.get("approval") or body.get("denial") or {}), "actor": approver}
        ports.audit(action, approval, started)
        from sonder_runtime.interfaces.http.chat_tool_stream import notify_call_decision

        notify_call_decision(str(approval.get("call_id") or target))
    handler._send_json_payload(dict(result.body), status=result.status)


__all__ = ["HttpApprovalPorts", "ROUTE", "serve_get", "serve_post"]
