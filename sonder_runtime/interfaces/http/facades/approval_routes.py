"""HTTP approval routes: list refused calls, approve one once, deny, revoke.

* ``GET /v1/approvals`` -- refused calls waiting for approval, and approvals;
* ``POST /v1/approvals/<call_id>`` -- approve one refused call once;
* ``POST /v1/approvals/revoke/<nonce>`` -- withdraw an open approval;
* ``POST /v1/approvals/deny/<call_id>`` -- decline one refused call (authorises
  nothing; revokes its open approval and lets a waiting turn see the refusal).

Reviewing the list needs a developer or administrator. Approving, denying or
revoking issues or changes authority another caller spends later, so it needs
exactly what the ``permission_approve`` tool needs on the chat ``/<tool>``
path: the administrator role its system operation is bound to (the single-user
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

import json
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


def approver_label(context: Any) -> str:
    """Who issued an HTTP approval, as the ledger records it.

    ``local-open`` for the single-user listener; ``<role>:<username>`` for an
    account (a dict or an object with ``role``/``username``), so the audit
    trail names the role that authorized the approval; ``admin-key`` for the
    deployment API key; ``http`` otherwise. Pure; safe from any thread.
    """
    if context.get("mode") == "local-open":
        return "local-open"
    account = context.get("account")
    if account:
        def field(name: str) -> str:
            value = account.get(name) if isinstance(account, dict) else getattr(account, name, "")
            return str(value or "").strip()

        return "%s:%s" % (field("role") or "account", field("username") or "account")
    if context.get("api_key"):
        return "admin-key"
    return "http"


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
        # The ledger is not scoped by owner, so in any mode with accounts the
        # exact code of someone else's pending call stays hidden until approvals
        # are bound to the turn's owner (design D7).
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
    """``POST /v1/approvals/<call_id>``, ``POST /v1/approvals/deny/<call_id>``
    and ``POST /v1/approvals/revoke/<nonce>``.

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

    def run():
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
    handler._send_json_payload(dict(result.body), status=result.status)


__all__ = ["HttpApprovalPorts", "ROUTE", "approver_label", "serve_get", "serve_post"]
