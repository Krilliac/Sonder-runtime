"""Durable, bounded linkage from jobs to model-visible session history.

This adapter deliberately owns no job state.  Callers inject the session
repository and submit immutable job revisions or bounded output events.  The
event key is persisted in the payload and also used to derive the repository
event id, allowing a new recorder after restart to replay linkage without
appending duplicates.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from collections.abc import Mapping
from typing import Any

from ..execution.world_control import OutputEvent
from ..ports.jobs import JobRecord
from ..ports.session_repository import SessionEvent, SessionRepository


_LIFECYCLE_EVENT_TYPES = frozenset({"job.created", "job.lifecycle"})
_OUTPUT_EVENT_TYPE = "job.output"
_DEFAULT_MAX_EVENTS = 10_000
_DEFAULT_MAX_OUTPUT_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class JobSessionLinkage:
    """A bounded result of recording or replaying one job linkage event."""

    job_id: str
    session_id: str
    event_key: str
    event: SessionEvent
    replayed: bool


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    return value


def _json_copy(value: Mapping[str, object], *, max_bytes: int) -> dict[str, object]:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > max_bytes:
            raise ValueError("payload exceeds the configured bound")
        copied = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        if str(exc) == "payload exceeds the configured bound":
            raise
        raise TypeError("payload must be JSON-serializable") from exc
    if not isinstance(copied, dict):
        raise TypeError("payload must be an object")
    return copied


class JobSessionLifecycleRecorder:
    """Record job lifecycle/output facts into a parent session stream.

    A job is linked only when ``JobIdentity.parent_session_id`` is present.
    Lifecycle revisions and output watermarks are naturally idempotent; an
    explicit ``event_key`` is available for callers replaying an external
    event whose key is not the default revision/watermark key.
    """

    def __init__(
        self,
        repository: SessionRepository,
        *,
        max_events: int = _DEFAULT_MAX_EVENTS,
        max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES,
    ) -> None:
        if not 1 <= max_events <= 100_000:
            raise ValueError("max_events must be between 1 and 100000")
        if not 1 <= max_output_bytes <= 2_000_000:
            raise ValueError("max_output_bytes must be between 1 and 2000000")
        self._repository = repository
        self._max_events = max_events
        self._max_output_bytes = max_output_bytes

    @staticmethod
    def _event_id(session_id: str, event_key: str) -> str:
        digest = hashlib.sha256(f"{session_id}:{event_key}".encode("utf-8")).hexdigest()
        return f"job_session_{digest}"

    def _existing(self, session_id: str, job_id: str, event_key: str) -> SessionEvent | None:
        for event in self._repository.search(session_id=session_id, text=event_key, limit=self._max_events):
            if event.payload.get("job_id") == job_id and event.payload.get("event_key") == event_key:
                return event
        return None

    def _append(
        self,
        *,
        session_id: str,
        job_id: str,
        event_key: str,
        event_type: str,
        payload: Mapping[str, object],
    ) -> JobSessionLinkage:
        existing = self._existing(session_id, job_id, event_key)
        if existing is not None:
            return JobSessionLinkage(job_id, session_id, event_key, existing, True)
        safe_payload = _json_copy(payload, max_bytes=self._max_output_bytes)
        safe_payload.update({"job_id": job_id, "event_key": event_key})
        event = self._repository.append(
            session_id,
            event_type,
            safe_payload,
            event_id=self._event_id(session_id, event_key),
        )
        return JobSessionLinkage(job_id, session_id, event_key, event, False)

    def record_lifecycle(
        self, record: JobRecord, *, event_key: str | None = None
    ) -> JobSessionLinkage | None:
        """Record one job revision, or return ``None`` for an unlinked job."""
        if not isinstance(record, JobRecord):
            raise TypeError("record must be a JobRecord")
        session_id = record.identity.parent_session_id
        if session_id is None:
            return None
        _required_text(session_id, "parent_session_id")
        job_id = _required_text(record.identity.job_id, "job_id")
        key = event_key or f"{job_id}:revision:{record.revision}"
        _required_text(key, "event_key")
        return self._append(
            session_id=session_id,
            job_id=job_id,
            event_key=key,
            event_type="job.created" if record.revision == 0 else "job.lifecycle",
            payload={
                "revision": record.revision,
                "status": record.status.value,
                "kind": record.identity.kind,
                "operation_id": record.identity.operation_id,
                "result": record.result,
                "error": record.error,
            },
        )

    def record_output(
        self,
        record: JobRecord,
        output: OutputEvent,
        *,
        event_key: str | None = None,
    ) -> JobSessionLinkage | None:
        """Record one bounded output watermark for a linked job."""
        if not isinstance(record, JobRecord):
            raise TypeError("record must be a JobRecord")
        if not isinstance(output, OutputEvent):
            raise TypeError("output must be an OutputEvent")
        session_id = record.identity.parent_session_id
        if session_id is None:
            return None
        _required_text(session_id, "parent_session_id")
        job_id = _required_text(record.identity.job_id, "job_id")
        if len(output.data.encode("utf-8")) > self._max_output_bytes:
            raise ValueError("output exceeds the configured bound")
        key = event_key or f"{job_id}:output:{output.watermark.sequence}"
        payload: dict[str, object] = {
            "revision": record.revision,
            "sequence": output.watermark.sequence,
            "stream": output.stream.value,
            "data": output.data,
        }
        if output.spill is not None:
            payload["spill"] = {
                "digest": output.spill.digest,
                "preview": output.spill.preview,
                "size": output.spill.size,
                "mime_type": output.spill.mime_type,
                "owner_id": output.spill.owner_id,
            }
        return self._append(
            session_id=session_id, job_id=job_id, event_key=key,
            event_type=_OUTPUT_EVENT_TYPE, payload=payload,
        )

    def replay(self, session_id: str, *, job_id: str | None = None) -> tuple[SessionEvent, ...]:
        """Reopen linkage from durable history without writing anything."""
        _required_text(session_id, "session_id")
        events = self._repository.read_range(session_id, limit=self._max_events)
        if job_id is not None:
            _required_text(job_id, "job_id")
            events = tuple(event for event in events if event.payload.get("job_id") == job_id)
        return tuple(event for event in events if event.event_type in _LIFECYCLE_EVENT_TYPES | {_OUTPUT_EVENT_TYPE})


class JobRegistryLifecycleAdapter:
    """Small application adapter for registry-owned lifecycle transitions."""

    def __init__(self, recorder: JobSessionLifecycleRecorder) -> None:
        if not isinstance(recorder, JobSessionLifecycleRecorder):
            raise TypeError("recorder must be a JobSessionLifecycleRecorder")
        self._recorder = recorder

    def record(self, record: JobRecord) -> JobSessionLinkage | None:
        """Persist one registry revision; unlinked jobs remain no-ops."""
        return self._recorder.record_lifecycle(record)

    def record_many(self, records: tuple[JobRecord, ...]) -> tuple[JobSessionLinkage, ...]:
        """Persist bounded registry results without changing their ordering."""
        linkages = [self._recorder.record_lifecycle(record) for record in records]
        return tuple(linkage for linkage in linkages if linkage is not None)


__all__ = ["JobRegistryLifecycleAdapter", "JobSessionLifecycleRecorder", "JobSessionLinkage"]
