"""Direct-tool and console one-shot approval helpers extracted from server.py.

Keeps ``server.py`` under the lint-ratchet module size cap without changing
behavior, authority checks, or audit records. All runtime dependencies arrive
as explicit callables/objects from ``server.py``.
"""
from __future__ import annotations

import contextlib
import json
import os
import time
from collections.abc import Callable
from typing import Any

from sonder_runtime.adapters.security import approval_ledger as approval_ledger_module


def approval_authority(
    token: Any,
    *,
    operator_attended: Any,
    account_from_token: Callable[[str], Any],
    require_role: Callable[[Any, str], tuple[bool, Any]],
) -> tuple[str, str]:
    """``(approver, "")`` when this caller may issue approvals, else ``("", why not)``."""
    if token is operator_attended:
        return "console operator", ""
    if isinstance(token, str) and token:
        account = account_from_token(token)
        ok, _ = require_role(account, "developer")
        if ok:
            if isinstance(account, dict):
                username = str(account.get("username") or "")
            else:
                username = str(getattr(account, "username", "") or "")
            return "developer:%s" % (username or "token"), ""
    env_ok = os.environ.get("SONDER_ALLOW_PERMISSION_EDITS", "").strip().lower() in (
        "1", "true", "yes", "on"
    )
    if env_ok:
        return "env:SONDER_ALLOW_PERMISSION_EDITS", ""
    return "", (
        "one-shot approvals need the console (/approve, after answering its "
        "prompt), a developer token, or SONDER_ALLOW_PERMISSION_EDITS=1."
    )


def approval_view(
    token: Any,
    *,
    operator_attended: Any,
    admin_require: Callable[[str], tuple[bool, Any]],
) -> Any:
    """All owners' calls only after an admin-level check (attended console, admin session token)."""
    admin = token is operator_attended or (
        isinstance(token, str) and bool(token) and admin_require(token)[0]
    )
    return approval_ledger_module.operator_scope() if admin else contextlib.nullcontext()


def parse_approve_command(
    arg: str,
    *,
    operator_approved: bool,
    operator_attended: Any,
) -> tuple[str | None, dict[str, Any]]:
    """Parse ``/approve`` syntax into ``(usage_or_none, permission_approve_kwargs)``."""
    usage = (
        "usage: /approve <call id> [ttl seconds]   approve one pending call once (see /approvals)\n"
        "       /approve revoke <nonce>            withdraw an open approval\n"
        "       /approve call <tool> <json>        approve one exact call before it is made"
    )
    text = str(arg or "").strip()
    if not text:
        return usage, {}
    token: Any = operator_attended if operator_approved else ""
    parts = text.split(None, 2)
    head = parts[0].lower()
    if head == "revoke":
        if len(parts) != 2:
            return usage, {}
        return None, {"revoke": parts[1], "token": token}
    if head == "call":
        if len(parts) != 3:
            return usage, {}
        return None, {"tool": parts[1], "arguments_json": parts[2], "token": token}
    ttl = approval_ledger_module.DEFAULT_TTL_SECONDS
    if len(parts) >= 2:
        try:
            ttl = int(parts[1])
        except ValueError:
            return usage, {}
    return None, {"call_id": head, "ttl_seconds": ttl, "token": token}


def dispatch_approve_command(
    arg: str,
    *,
    operator_approved: bool,
    operator_attended: Any,
    approve_fn: Callable[..., str],
) -> str:
    """Parse ``/approve`` syntax and delegate to ``permission_approve``."""
    usage, kwargs = parse_approve_command(
        arg, operator_approved=operator_approved, operator_attended=operator_attended,
    )
    if usage is not None:
        return usage
    return approve_fn(**kwargs)


def execute_permission_approve(
    *,
    call_id: str,
    tool: str,
    arguments_json: str,
    ttl_seconds: int,
    revoke: str,
    token: Any,
    started: float,
    operator_attended: Any,
    authority_fn: Callable[[Any], tuple[str, str]],
    view_fn: Callable[[Any], Any],
    permission_modes: Any,
    record_direct_tool: Callable[..., Any],
) -> str:
    """Run ``permission_approve`` over the approval ledger with full owner & preview checks."""
    approver, why_not = authority_fn(token)
    if not approver:
        error = "ERROR: %s" % why_not
        return error
    ledger = permission_modes.approval_ledger()
    if ledger is None:
        error = "ERROR: one-shot approvals are not available in this process."
        return error
    surface = "console" if token is operator_attended else "tool"
    with view_fn(token):
        if revoke:
            approval = ledger.revoke(revoke)
            if approval is None:
                error = (
                    "ERROR: no open approval %s (it may be spent, expired, already "
                    "revoked, or mistyped; see /approvals)." % revoke
                )
                return error
            ref = approval_ledger_module.call_ref(approval.digest, approval.owner)
            record_direct_tool(
                "permission_approve",
                {"revoke": approval.nonce, "owner": ref.split("@", 1)[1]},
                ok=True,
                started=started,
                summary="revoked %s call %s" % (approval.tool, ref),
            )
            return "revoked %s (%s call %s)." % (approval.nonce, approval.tool, ref)
        try:
            if call_id:
                pending = ledger.resolve_call(call_id)
                if not pending.complete:
                    raise approval_ledger_module.ApprovalLedgerError(
                        "call %s's preview does not show everything the approval "
                        "would cover (%s); approve it with /approve call %s <arguments "
                        "json> giving its full arguments" % (
                            pending.call_id,
                            pending.preview.split("]", 1)[0].lstrip("["),
                            pending.tool,
                        )
                    )
                target, digest, preview, owner = (
                    pending.tool,
                    pending.digest,
                    pending.preview,
                    pending.owner,
                )
            elif tool and arguments_json:
                try:
                    arguments = json.loads(arguments_json)
                except ValueError as exc:
                    raise approval_ledger_module.ApprovalLedgerError(
                        "arguments_json must be a JSON object: %s" % exc
                    ) from exc
                if not isinstance(arguments, dict):
                    raise approval_ledger_module.ApprovalLedgerError(
                        "arguments_json must be a JSON object"
                    )
                target, owner = str(tool or "").strip().lstrip("/"), None
                digest = permission_modes.call_digest(target, arguments)
                preview = permission_modes.preview_fields(arguments)
                if not digest:
                    raise approval_ledger_module.ApprovalLedgerError(
                        "that call has no canonical form and cannot be approved"
                    )
            else:
                raise approval_ledger_module.ApprovalLedgerError(
                    "give a call id from /approvals, or a tool with its arguments_json"
                )
            approval = ledger.issue(
                target,
                digest,
                approver=approver,
                surface=surface,
                ttl_seconds=ttl_seconds,
                preview=preview,
                override_denial=surface == "console",
                owner=owner,
            )
            overrode = approval.overrode_denial
        except approval_ledger_module.ApprovalLedgerError as exc:
            error = "ERROR: %s" % exc
            return error
    record_direct_tool(
        "permission_approve",
        {
            "tool": approval.tool,
            "call_id": approval.call_id,
            "ttl_seconds": int(ttl_seconds),
            "owner": approval_ledger_module.owner_tag(approval.owner),
            **({"overrode_denial": True} if overrode else {}),
        },
        ok=True,
        started=started,
        summary=approval.nonce,
    )
    return (
        ("overrode a denial: " if overrode else "")
        + "approved %s call %s once%s: nonce %s, by %s, expires %s.\n"
        "  the next unchanged %s call from any surface runs once and spends it; "
        "/approvals shows it; /approve revoke %s withdraws it."
        % (
            approval.tool,
            approval.call_id,
            " for owner " + approval_ledger_module.owner_tag(approval.owner) if approval.owner else "",
            approval.nonce,
            approval.approver,
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(approval.expires_ts)),
            approval.tool,
            approval.nonce,
        )
    )


__all__ = [
    "approval_authority",
    "approval_view",
    "dispatch_approve_command",
    "execute_permission_approve",
    "parse_approve_command",
]
