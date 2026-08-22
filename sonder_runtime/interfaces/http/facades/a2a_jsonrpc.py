"""Authenticated HTTP presentation for the bounded A2A JSON-RPC seam."""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

from ....application.chat.handle_chat import ChatCommand
from ....application.context import local_owner_context
from ....application.ports.jobs import JobIdentity, JobStatus
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
    """Bind bounded A2A task operations to existing application services.

    ``SendMessage`` is deliberately a synchronous, local-owner bridge.  It
    admits text-only user messages, records the request in the durable job
    registry, and invokes the existing typed chat service under an explicit
    operation context.  It does not claim remote delegation, multimodal
    support, or background execution.
    """
    if not isinstance(base_url, str) or not base_url.strip():
        return None
    jobs = getattr(application, "job_service", None)
    registry_factory = getattr(application, "agent_registry", None)
    if not callable(jobs) or not callable(registry_factory):
        return None
    card_facade = card_facade or A2AAgentCardFacade()

    def task_payload(record):
        if record is None:
            raise ValueError("task not found")
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
        task = {
            "id": identity.job_id,
            "contextId": identity.parent_session_id or "sonder-runtime",
            "status": status,
        }
        result = getattr(record, "result", None)
        if isinstance(result, Mapping) and isinstance(result.get("response_text"), str):
            response_text = result["response_text"]
            digest = hashlib.sha256(response_text.encode("utf-8")).hexdigest()
            task["artifacts"] = [{
                "artifactId": f"{identity.job_id}-response",
                "parts": [{"text": response_text}],
                "lastChunk": True,
                "metadata": {
                    "mimeType": "text/plain",
                    "sha256": digest,
                },
            }]
        return task

    def message_text(params):
        message = params.get("message")
        if not isinstance(message, Mapping):
            raise ValueError("message is required")
        if message.get("role", "ROLE_USER") not in {"ROLE_USER", "user"}:
            raise ValueError("only user messages are accepted")
        message_id = message.get("messageId")
        if not isinstance(message_id, str) or not message_id.strip() or len(message_id) > 128:
            raise ValueError("messageId must be a non-empty string of at most 128 characters")
        parts = message.get("parts")
        if not isinstance(parts, list) or not 1 <= len(parts) <= 32:
            raise ValueError("message.parts must contain between 1 and 32 parts")
        texts = []
        for part in parts:
            if not isinstance(part, Mapping) or set(part) != {"text"} or not isinstance(part["text"], str):
                raise ValueError("only text message parts are supported")
            texts.append(part["text"])
        content = "".join(texts)
        if not content.strip() or len(content) > 64_000:
            raise ValueError("message text must be non-empty and at most 64000 characters")
        return message_id, content

    def send_message(params):
        message_id, content = message_text(params)
        chat = getattr(application, "chat", None)
        if chat is None or not callable(getattr(chat, "complete", None)):
            raise ValueError("A2A chat admission is not configured")
        job_id = "a2a-" + hashlib.sha256(message_id.encode("utf-8")).hexdigest()[:32]
        service = jobs()
        existing = service.get(job_id)
        if existing is not None:
            return {"task": task_payload(existing)}
        identity = JobIdentity(
            job_id=job_id,
            kind="a2a.chat",
            operation_id=message_id,
            idempotency_key=message_id,
        )
        service.start(identity)
        worker_id = f"a2a-http-{uuid.uuid4().hex}"
        service.claim(job_id, worker_id, lease_seconds=300)
        try:
            result = chat.complete(
                ChatCommand(content=content, tier="sonder"),
                local_owner_context(
                    correlation_id=message_id,
                    source="http",
                    auth_level="admin",
                    timeout_seconds=300,
                    cloud_allowed=False,
                ),
            )
            record = service.finish(
                job_id,
                worker_id,
                JobStatus.SUCCEEDED,
                result={
                    "response_text": result.response_text,
                    "model": result.model,
                    "tier": result.tier,
                },
            )
        except Exception as error:
            record = service.finish(
                job_id,
                worker_id,
                JobStatus.FAILED,
                error=f"{type(error).__name__}: {error}"[:1024],
            )
        return {"task": task_payload(record)}

    def handler(method, params):
        if method == "SendMessage":
            return send_message(params)
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
