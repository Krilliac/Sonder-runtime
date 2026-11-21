"""HTTP one-shot approvals: the structured refusal and the approve-once routes.

Two halves of one flow (app plan S1/S2):

* ``refusal_receipt`` turns an unattended permission refusal that named a
  call (``Decision.call_id``) into the ``sonder_receipt.refusal`` object a
  chat response carries, so a client can show *what* was refused and offer
  "approve this call once" without parsing prose. ``turn_refusal_receipt``
  builds it for a turn from the permission-policy port serve.py passes in.
* ``approvals_payload`` / ``approve_call`` / ``revoke_approval`` serve
  ``GET /v1/approvals``, ``POST /v1/approvals/<call_id>`` and
  ``POST /v1/approvals/revoke/<nonce>`` over the same approval ledger the
  permission gate spends from.

The ledger is passed in (anything with ``pending``, ``approvals``,
``resolve_call``, ``issue`` and ``revoke``), so this module stays free of
adapters; authentication, the role check and auditing stay with the HTTP
adapter that calls it.

What an HTTP approval can and cannot do:

* It approves only a call that was actually refused and is still pending:
  there is no "approve these arguments in advance" over HTTP.
* It is bound to that call's full digest (tool name plus canonical,
  credential-free arguments). The path takes the 16-character call id or the
  full 64-character digest, never a shorter prefix, and a body ``digest`` or
  ``tool`` that disagrees with the pending call is refused.
* At most one open approval exists per call: a second POST for a call that
  already has an open approval is refused with that approval, so a retried
  request can never let the call run twice.
* The ledger spends it atomically on first use and it expires; revoking
  withdraws it.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Mapping

from .local_open_authority import AUTHENTICATED_ADMIN_REMEDY, credential_less

__all__ = [
    "ApprovalResult",
    "CALL_ID_CHARS",
    "approval_view",
    "approvals_payload",
    "approve_call",
    "pending_view",
    "refusal_receipt",
    "revoke_approval",
    "turn_refusal_receipt",
]

CALL_ID_CHARS = 16
DIGEST_CHARS = 64
DEFAULT_TTL_SECONDS = 900
_HEX = frozenset("0123456789abcdef")
_NONCE_PREFIX = "apv_"
_MAX_NONCE_CHARS = 64
_BODY_KEYS = frozenset({"ttl_seconds", "tool", "digest"})
# Issue is check-then-insert; one lock per process keeps "one open approval
# per call" true under concurrent requests on this listener.
_ISSUE_LOCK = threading.Lock()
# The 403 codes a credential-less (local-open) caller gets from the two HTTP
# grants a refusal would otherwise offer. They restate approval_routes'
# GRANT_REFUSED_CODE (that module imports this one, so it cannot be imported
# here) and permission_mode_post's ESCALATION_CODE; a test holds each pair
# equal.
_GRANT_REFUSED_CODE = "APPROVAL_REQUIRES_AUTHENTICATED_ADMIN"
_ESCALATION_REFUSED_CODE = "PERMISSION_MODE_ESCALATION_REFUSED"


@dataclass(frozen=True)
class ApprovalResult:
    status: int
    body: Mapping[str, Any]


def _iso(ts) -> str | None:
    if not ts:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ts)))


def _error(status: int, code: str, message: str, **extra) -> ApprovalResult:
    kind = "not_found" if status == 404 else (
        "server_error" if status >= 500 else "invalid_request")
    return ApprovalResult(status, {"error": {
        "message": message, "type": kind, "code": code, **extra,
    }})


def refusal_receipt(decision, *, mode_label: str = "", modes_allowing=(),
                    count: int = 1, credential_less: bool = False,
                    raises=()) -> dict | None:
    """``sonder_receipt.refusal`` for one unattended refusal that named a call.

    ``None`` when the decision is not such a refusal. The reason is the gate's
    own prose (it already names every remedy); ``remedies`` restates them as
    data a client can act on. Arguments never appear: only the tool, the
    call id and the mode.

    ``credential_less`` is ``local_open_authority.credential_less`` for the
    request that was refused. Such a caller is refused both HTTP grants
    (approving the call; raising the mode), so the receipt must not offer
    them: ``approve_once`` then carries no ``method``/``path``, only the
    console command, and is marked ``http_available: false`` with the code
    the POST would answer; a ``switch_mode`` to any mode in ``raises`` (the
    modes of ``modes_allowing`` that would be a raise from the refusing mode)
    is marked the same way; and an ``authenticate`` remedy names what
    restores the HTTP grants. Pure; safe from any thread.
    """
    call = str(getattr(decision, "call_id", "") or "")
    if (
        getattr(decision, "action", "") != "deny"
        or getattr(decision, "source", "") != "unattended"
        or not call
    ):
        return None
    tool = str(getattr(decision, "tool", "") or "")
    mode = str(getattr(decision, "mode", "") or "")
    modes = [m for m in (modes_allowing or ()) if m and m != mode]
    remedies: list[dict]
    if credential_less:
        remedies = [
            {"kind": "approve_once", "call_id": call, "console": "/approve %s" % call,
             "http_available": False, "http_refusal": _GRANT_REFUSED_CODE},
        ]
        refused = set(raises or ())
        lowering = [m for m in modes if m not in refused]
        raising = [m for m in modes if m in refused]
        if lowering:
            remedies.append({"kind": "switch_mode", "modes": lowering})
        if raising:
            remedies.append({
                "kind": "switch_mode", "modes": raising,
                "console": "/mode %s" % raising[0],
                "http_available": False, "http_refusal": _ESCALATION_REFUSED_CODE,
            })
    else:
        remedies = [
            {"kind": "approve_once", "call_id": call, "method": "POST",
             "path": "/v1/approvals/%s" % call, "console": "/approve %s" % call},
        ]
        if modes:
            remedies.append({"kind": "switch_mode", "modes": modes})
    remedies.append({"kind": "allow_rule", "console": "/permissions"})
    remedies.append({"kind": "console", "detail": "run it from the console and answer the prompt"})
    if credential_less:
        remedies.append({"kind": "authenticate", "detail": AUTHENTICATED_ADMIN_REMEDY})
    receipt = {
        "kind": "refused",
        "tool": tool,
        "call_id": call,
        "risk": str(getattr(decision, "risk", "") or ""),
        "mode": mode,
        "mode_label": mode_label or mode,
        "reason": str(getattr(decision, "reason", "") or ""),
        "remedies": remedies,
    }
    if count > 1:
        # Only the last refusal is described; say that there were others.
        receipt["refusals_in_turn"] = int(count)
    return receipt


def modes_raising_from(policy, current, modes) -> list:
    """The modes of *modes* a credential-less caller may not select over HTTP.

    ``policy`` is the permission-policy port; its ``is_unattended_escalation``
    is the rule ``POST /v1/permission-mode`` applies under local-open. Fails
    closed: when the policy cannot say, every mode counts as a raise.
    """
    try:
        return [m for m in modes if policy.is_unattended_escalation(current, m)]
    except Exception:
        return list(modes)


def turn_refusal_receipt(refusals, context, policy) -> dict | None:
    """The structured refusal for the last nameable refusal of a turn, or None.

    ``refusals`` are the turn's captured unattended refusals, ``context`` the
    request's auth context and ``policy`` the permission-policy port. Under
    local-open (and for a missing context, which fails closed) the receipt
    marks the HTTP approval and a mode raise unavailable, as those routes
    refuse such a caller. Safe from any thread; reads the policy only.
    """
    if not refusals:
        return None
    decision = refusals[-1]
    try:
        label = policy.mode_label(decision.mode)
    except Exception:
        label = str(decision.mode or "")
    try:
        modes = list(policy._modes_allowing(decision.risk))
    except Exception:
        modes = []
    return refusal_receipt(
        decision, mode_label=label, modes_allowing=modes,
        count=len(refusals), credential_less=credential_less(context),
        raises=modes_raising_from(policy, decision.mode, modes),
    )


def pending_view(item) -> dict:
    return {
        "call_id": str(item.call_id),
        "digest": str(item.digest),
        "tool": str(item.tool),
        "surface": str(item.surface or ""),
        "preview": str(item.preview or ""),
        "count": int(item.count),
        "first_at": _iso(item.first_ts),
        "last_at": _iso(item.last_ts),
    }


def approval_view(item, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    state = item.state(now) if callable(getattr(item, "state", None)) else "open"
    return {
        "nonce": str(item.nonce),
        "tool": str(item.tool),
        "call_id": str(item.call_id),
        "digest": str(item.digest),
        "approver": str(item.approver),
        "surface": str(item.surface or ""),
        "preview": str(item.preview or ""),
        "state": state,
        "issued_at": _iso(item.issued_ts),
        "expires_at": _iso(item.expires_ts),
        "ttl_seconds": max(0, int(round(float(item.expires_ts) - float(item.issued_ts)))),
        "consumed_at": _iso(getattr(item, "consumed_ts", None)),
        "consumed_surface": str(getattr(item, "consumed_surface", "") or ""),
        "revoked_at": _iso(getattr(item, "revoked_ts", None)),
    }


def approvals_payload(ledger, *, limit: int = 20, include_spent: bool = False) -> dict:
    """Pending (refused, approvable) calls and approvals; previews are pre-redacted."""
    limit = max(1, min(int(limit), 200))
    now = time.time()
    return {
        "object": "approvals",
        "pending": [pending_view(item) for item in ledger.pending(limit)],
        "approvals": [
            approval_view(item, now)
            for item in ledger.approvals(include_spent=include_spent, limit=limit)
        ],
    }


def _valid_call_ref(value: str) -> bool:
    return len(value) in (CALL_ID_CHARS, DIGEST_CHARS) and all(c in _HEX for c in value)


def _ttl(value) -> int | None:
    if value is None:
        return DEFAULT_TTL_SECONDS
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def approve_call(ledger, call_ref: str, request, *, approver: str,
                 surface: str = "http") -> ApprovalResult:
    """Approve exactly the pending call ``call_ref`` names, once.

    ``request`` is the decoded JSON body (``{}`` when empty): optional
    ``ttl_seconds`` (60..86400, default 900), and optional ``tool`` and
    ``digest`` which, when given, must equal the pending call's.
    """
    ref = str(call_ref or "").strip().lower()
    if not _valid_call_ref(ref):
        return _error(400, "INVALID_CALL_ID",
                      "a call id is the 16 hex characters a refusal names "
                      "(or the full 64-character digest)")
    if request is None:
        request = {}
    if not isinstance(request, Mapping):
        return _error(400, "INVALID_REQUEST", "the request body must be a JSON object")
    unknown = sorted(set(request) - _BODY_KEYS)
    if unknown:
        return _error(400, "INVALID_REQUEST",
                      "unknown field(s): %s" % ", ".join(str(k) for k in unknown))
    ttl = _ttl(request.get("ttl_seconds"))
    if ttl is None:
        return _error(400, "INVALID_TTL", "ttl_seconds must be a whole number of seconds")
    try:
        pending = ledger.resolve_call(ref)
    except ValueError:
        return _error(404, "CALL_NOT_PENDING",
                      "no refused call %s is waiting for approval; it may already "
                      "have been approved and run, or have aged out" % ref)
    wanted_digest = request.get("digest")
    if wanted_digest is not None and str(wanted_digest).strip().lower() != pending.digest:
        return _error(409, "CALL_DIGEST_MISMATCH",
                      "the call's arguments changed since it was refused; "
                      "the new call needs its own approval")
    wanted_tool = request.get("tool")
    if wanted_tool is not None and str(wanted_tool).strip().lstrip("/") != pending.tool:
        return _error(409, "CALL_DIGEST_MISMATCH",
                      "call %s is a %s call, not %s" % (pending.call_id, pending.tool, wanted_tool))
    with _ISSUE_LOCK:
        now = time.time()
        for existing in ledger.approvals(include_spent=False, limit=200):
            if existing.digest == pending.digest and existing.tool == pending.tool:
                result = _error(409, "APPROVAL_ALREADY_OPEN",
                                "call %s already has an open approval" % pending.call_id)
                result.body["error"]["approval"] = approval_view(existing, now)
                return result
        try:
            approval = ledger.issue(
                pending.tool, pending.digest, approver=approver, surface=surface,
                ttl_seconds=ttl, preview=pending.preview,
            )
        except ValueError as exc:
            code = "INVALID_TTL" if "ttl" in str(exc) else "INVALID_REQUEST"
            return _error(400, code, str(exc))
    view = approval_view(approval)
    return ApprovalResult(201, {
        "object": "approval",
        "approval": view,
        "message": (
            "approved %s call %s once; the next unchanged call from any surface "
            "runs once and spends it (expires %s)"
            % (approval.tool, approval.call_id, view["expires_at"])
        ),
    })


def revoke_approval(ledger, nonce: str) -> ApprovalResult:
    """Withdraw one open approval by its nonce."""
    value = str(nonce or "").strip()
    body = value[len(_NONCE_PREFIX):]
    if (
        not value.startswith(_NONCE_PREFIX)
        or not body
        or len(value) > _MAX_NONCE_CHARS
        or any(c not in _HEX for c in body)
    ):
        return _error(400, "INVALID_NONCE", "an approval nonce looks like apv_<hex>")
    approval = ledger.revoke(value)
    if approval is None:
        return _error(404, "APPROVAL_NOT_OPEN",
                      "no open approval %s (it may be spent, expired or already revoked)" % value)
    return ApprovalResult(200, {
        "object": "approval",
        "approval": approval_view(approval),
        "message": "revoked %s (%s call %s)" % (approval.nonce, approval.tool, approval.call_id),
    })
