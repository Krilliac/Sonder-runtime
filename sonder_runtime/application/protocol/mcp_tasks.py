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
from ...domain.common.errors import InvalidInput


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


__all__ = ["McpTaskStatus", "McpTaskView", "project_job"]
