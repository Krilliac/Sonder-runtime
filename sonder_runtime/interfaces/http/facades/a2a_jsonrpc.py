"""Authenticated HTTP presentation for the bounded A2A JSON-RPC seam."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ...a2a.jsonrpc import A2AJsonRpcTransport
from .a2a import A2AAgentCardFacade


@dataclass(frozen=True, slots=True)
class A2AJsonRpcHttpResult:
    body: dict[str, Any]
    status_code: int = 200


def dispatch_a2a_jsonrpc_route(
    handler: object,
    method: str,
    path: str,
    payload: Mapping[str, Any] | str | bytes,
) -> A2AJsonRpcHttpResult | None:
    """Dispatch the explicitly configured local A2A JSON-RPC endpoint."""
    if path != "/a2a":
        return None
    if method != "POST":
        return A2AJsonRpcHttpResult(
            {"error": {"code": -32600, "message": "method not allowed"}}, 405
        )
    if not callable(handler):
        return A2AJsonRpcHttpResult(
            {"error": {"code": "A2A_UNAVAILABLE", "message": "A2A handler is not configured"}},
            503,
        )
    response = A2AJsonRpcTransport(handler).handle(payload)
    return A2AJsonRpcHttpResult(response)


def build_application_a2a_handler(
    application: object,
    *,
    base_url: str,
    card_facade: A2AAgentCardFacade | None = None,
):
    """Bind safe A2A reads/cancellation to existing application services.

    Message admission is intentionally not synthesized here: without a
    typed model-to-task admission service, ``SendMessage`` remains an explicit
    unsupported operation rather than an untracked background request.
    """
    if not isinstance(base_url, str) or not base_url.strip():
        return None
    jobs = getattr(application, "job_service", None)
    registry_factory = getattr(application, "agent_registry", None)
    if not callable(jobs) or not callable(registry_factory):
        return None
    card_facade = card_facade or A2AAgentCardFacade()

    def task_payload(record):
        state = {
            "pending": "TASK_STATE_WORKING",
            "claimed": "TASK_STATE_WORKING",
            "running": "TASK_STATE_WORKING",
            "paused": "TASK_STATE_INPUT_REQUIRED",
            "succeeded": "TASK_STATE_COMPLETED",
            "failed": "TASK_STATE_FAILED",
            "cancelled": "TASK_STATE_CANCELED",
            "interrupted": "TASK_STATE_FAILED",
        }.get(getattr(record.status, "value", str(record.status)), "TASK_STATE_FAILED")
        status = {"state": state}
        if getattr(record, "error", ""):
            status["message"] = {"role": "ROLE_AGENT", "parts": [{"text": "task failed"}]}
        identity = record.identity
        return {
            "id": identity.job_id,
            "contextId": identity.parent_session_id or "sonder-runtime",
            "status": status,
        }

    def handler(method, params):
        if method == "GetExtendedAgentCard":
            card = card_facade.card(
                registry_factory().registrations,
                base_url=base_url,
            )
            return {"agentCard": card.to_dict(), "digest": card.digest}
        if method == "GetTask":
            task_id = params.get("id")
            if not isinstance(task_id, str) or not task_id.strip():
                raise ValueError("task id is required")
            return {"task": task_payload(jobs().get(task_id))}
        if method == "ListTasks":
            page_size = params.get("pageSize", 100)
            if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 100:
                raise ValueError("pageSize must be between 1 and 100")
            records = jobs().list(limit=page_size)
            return {
                "tasks": [task_payload(record) for record in records],
                "totalSize": len(records),
                "pageSize": page_size,
                "nextPageToken": "",
            }
        if method == "CancelTask":
            task_id = params.get("id")
            if not isinstance(task_id, str) or not task_id.strip():
                raise ValueError("task id is required")
            records = jobs().cancel(task_id, reason="A2A cancellation")
            if not records:
                raise ValueError("task cancellation returned no record")
            return {"task": task_payload(records[-1])}
        raise ValueError(f"A2A method {method} is not configured")

    return handler


__all__ = [
    "A2AJsonRpcHttpResult",
    "build_application_a2a_handler",
    "dispatch_a2a_jsonrpc_route",
]
