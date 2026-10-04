"""Consume a journal-settled tool result without invoking the tool again."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..execution.effect_journal import (
    EffectJournalError, EffectState, JournalBinding, SettledEffectReplay,
)
from ..execution.gateway_calls import gateway_request_digest
from .receipt_digest import receipt_digest


def consume_settled_receipt(
    binding: JournalBinding, replay: SettledEffectReplay,
    request: Any, audit: object,
) -> dict[str, Any]:
    """Require the exact retained audit output and its terminal journal proof.

    The current gateway has already applied schema, permissions, approval,
    cancellation and deadline checks. Historical scope must match too; a
    retained receipt never grants access to another request or workspace.
    """
    get_intent = getattr(binding.journal, "get", None)
    read_receipt = getattr(audit, "read_receipt", None)
    record = get_intent(replay.intent_id) if callable(get_intent) else None
    if record is None or not callable(read_receipt) or (
        record.run_id != binding.run_id
        or record.worker_id != binding.worker_id
        or record.scope != binding.scope
        or record.idempotency_key != replay.idempotency_key
        or record.receipt_key != replay.receipt_key
        or record.state not in {EffectState.COMPLETED, EffectState.FAILED}
        or record.request_digest != gateway_request_digest(
            request.tool_name, request.arguments,
            request.permission.effects or frozenset({"unknown"}),
        )
    ):
        raise EffectJournalError("settled tool output has no matching journal proof")
    entry = read_receipt(replay.receipt_key)
    if not isinstance(entry, Mapping):
        raise EffectJournalError("settled tool output is no longer retained in the durable audit")
    expected = {
        "schema": "tool-audit-record-v2",
        "request_id": replay.receipt_key,
        "tool_name": request.tool_name,
        "principal_id": request.scope.principal_id,
        "workspace_roots": list(request.scope.workspace_roots),
        "source": request.scope.source,
        "auth_level": request.scope.auth_level,
        "session_id": request.session_id,
        "project_id": request.project_id,
        "execution_world": request.execution_world,
        "argument_digest": receipt_digest(dict(request.arguments)),
        "effects": sorted(request.permission.effects),
        "success": record.state is EffectState.COMPLETED,
        "result_digest": record.outcome_digest,
    }
    if any(entry.get(key) != value for key, value in expected.items()) or (
        "output" not in entry
        or receipt_digest(entry["output"]) != record.outcome_digest
        or type(entry.get("success")) is not bool
        or entry.get("terminal") != (
            "completed" if record.state is EffectState.COMPLETED else
            "cancelled" if entry.get("error_code") in {"TEST_CANCELLED", "CANCELLED", "Cancelled"}
            else "failed"
        )
        or not isinstance(entry.get("evidence"), Mapping)
        or entry.get("tool_schema_selection") != (
            request.schema_selection.marker() if request.schema_selection else None
        )
    ):
        raise EffectJournalError("settled tool output does not match its durable receipt")
    return {
        "request_id": request.request_id, "tool_name": request.tool_name,
        "success": entry["success"], "output": entry["output"],
        "error_code": entry["error_code"], "error": entry["error"],
        "duration_ms": entry["duration_ms"], "redaction_applied": entry["redaction_applied"],
        "approval_required": entry["approval_required"],
        "requester_id": request.scope.principal_id,
        "argument_digest": entry["argument_digest"], "result_digest": entry["result_digest"],
        "execution_world": entry["execution_world"], "policy_match": entry["policy_match"],
        "resource": {
            "principal_id": request.scope.principal_id,
            "workspace_roots": tuple(request.scope.workspace_roots),
            "source": request.scope.source, "auth_level": request.scope.auth_level,
        },
        "effects": tuple(entry["effects"]), "model": entry["model"], "terminal": entry["terminal"],
        "evidence": {**entry["evidence"], "replayed_from": replay.receipt_key},
    }


__all__ = ["consume_settled_receipt"]
