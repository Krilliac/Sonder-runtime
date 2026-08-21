"""MCP Tasks-shaped projection over Sonder's durable job identity.

This is a protocol-neutral application contract. It intentionally does not
start jobs, poll in a background thread, or expose result content; a transport
adapter can use the stable handle to call the existing durable job service.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json

from ..ports.jobs import JobRecord, JobStatus
from ...domain.common.errors import InvalidInput, NotFound


class McpTaskInvalidInput(InvalidInput):
    """Protocol-owned invalid MCP Tasks request."""


class McpTaskNotFound(NotFound):
    """Protocol-owned missing MCP task."""


class McpTaskStatus(str, Enum):
    WORKING = "working"
    INPUT_REQUIRED = "input_required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


def _status(record: JobRecord, *, input_required: bool) -> McpTaskStatus:
    if input_required and not record.is_terminal:
        return McpTaskStatus.INPUT_REQUIRED
    if record.status is JobStatus.SUCCEEDED:
        return McpTaskStatus.COMPLETED
    if record.status is JobStatus.FAILED:
        return McpTaskStatus.FAILED
    if record.status is JobStatus.CANCELLED:
        return McpTaskStatus.CANCELLED
    return McpTaskStatus.WORKING


@dataclass(frozen=True, slots=True)
class McpTaskView:
    """Reconnectable task metadata with content deliberately withheld."""

    task_id: str
    status: McpTaskStatus
    revision: int
    created_at: str
    updated_at: str
    result_available: bool
    error_present: bool
    input_required: bool = False
    expires_at: str | None = None
    poll_after_ms: int = 250

    def to_dict(self) -> dict[str, object]:
        return {
            "taskId": self.task_id,
            "status": self.status.value,
            "statusMessage": "input required" if self.input_required else None,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "revision": self.revision,
            "resultAvailable": self.result_available,
            "errorPresent": self.error_present,
            "expiresAt": self.expires_at,
            "pollAfterMs": self.poll_after_ms,
            "contentRedacted": True,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


def project_job(
    record: JobRecord,
    *,
    input_required: bool = False,
    expires_at: str | None = None,
    poll_after_ms: int = 250,
) -> McpTaskView:
    """Map one durable job to an MCP Tasks-compatible status handle."""
    if not isinstance(record, JobRecord):
        raise InvalidInput("MCP task projection requires a JobRecord")
    if type(input_required) is not bool:
        raise InvalidInput("input_required must be boolean")
    if expires_at is not None and type(expires_at) is not str:
        raise InvalidInput("expires_at must be a string or null")
    if type(poll_after_ms) is not int or isinstance(poll_after_ms, bool):
        raise InvalidInput("poll_after_ms must be an integer")
    if not 0 <= poll_after_ms <= 86_400_000:
        raise InvalidInput("poll_after_ms is out of bounds")
    return McpTaskView(
        task_id=record.identity.job_id,
        status=_status(record, input_required=input_required),
        revision=record.revision,
        created_at=record.created_at,
        updated_at=record.updated_at,
        result_available=record.result is not None,
        error_present=bool(record.error),
        input_required=input_required and not record.is_terminal,
        expires_at=expires_at,
        poll_after_ms=poll_after_ms,
    )


class McpTaskHandler:
    """Adapt the durable job service to the negotiated MCP Tasks methods.

    The handler is deliberately content-blind: MCP receives only the stable
    task view and never the job result or error text.  ``tasks/update`` is a
    read-through projection for transports that receive a provider update;
    state mutation remains owned by the durable job service.
    """

    def __init__(self, jobs, *, poll_after_ms: int = 250) -> None:
        if not callable(getattr(jobs, "get", None)):
            raise InvalidInput("MCP Tasks handler requires a job get service")
        if not callable(getattr(jobs, "cancel", None)):
            raise InvalidInput("MCP Tasks handler requires a job cancel service")
        if type(poll_after_ms) is not int or isinstance(poll_after_ms, bool):
            raise InvalidInput("poll_after_ms must be an integer")
        if not 0 <= poll_after_ms <= 86_400_000:
            raise InvalidInput("poll_after_ms is out of bounds")
        self._jobs = jobs
        self._poll_after_ms = poll_after_ms

    def __call__(self, method: str, params: dict[str, object]) -> dict[str, object]:
        try:
            if method not in {"tasks/get", "tasks/update", "tasks/cancel"}:
                raise InvalidInput("unsupported MCP Tasks method")
            if not isinstance(params, dict):
                raise InvalidInput("MCP Tasks parameters must be an object")
            task_id = params.get("taskId")
            if not isinstance(task_id, str) or not task_id.strip():
                raise InvalidInput("taskId is required")

            if method == "tasks/cancel":
                reason = params.get("reason", "cancelled")
                if not isinstance(reason, str) or not reason.strip():
                    raise InvalidInput("cancel reason must be a non-empty string")
                records = tuple(self._jobs.cancel(task_id, reason=reason))
                record = records[-1] if records else self._jobs.get(task_id)
            else:
                record = self._jobs.get(task_id)
            return project_job(
                record,
                input_required=params.get("inputRequired", False),
                expires_at=params.get("expiresAt"),
                poll_after_ms=self._poll_after_ms,
            ).to_dict()
        except NotFound as exc:
            raise McpTaskNotFound(str(exc)) from exc
        except InvalidInput as exc:
            raise McpTaskInvalidInput(str(exc)) from exc


__all__ = [
    "McpTaskHandler", "McpTaskInvalidInput", "McpTaskNotFound",
    "McpTaskStatus", "McpTaskView", "project_job",
]
