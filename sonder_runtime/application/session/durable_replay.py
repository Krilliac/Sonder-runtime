"""Crash-safe session durability and complete model-visible reconstruction.

This boundary deliberately depends on the session repository port only.  A
replay is permitted to expose state after the repository has proved the whole
append-only chain; a partial or tampered stream is never silently projected.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from types import MappingProxyType
from typing import Mapping, Protocol, Sequence

from ...domain.common.errors import IntegrityFailure, InvalidInput
from ...domain.common.events import DomainEvent
from ..ports.model_gateway import ModelRequest
from ..ports.session_repository import IntegrityReport, SessionEvent
from .replay import SessionReplay, replay_session


class AppendOnlySessionRepository(Protocol):
    """Minimum durable adapter contract for crash-safe session replay.

    Implementations must commit each append before returning, assign one
    monotonic sequence per session, and make history immutable.  Update and
    delete are intentionally absent from this port.
    """

    def append(
        self, session_id: str, event_type: str, payload: Mapping[str, object], *,
        event_id: str | None = None, occurred_at_utc: str | None = None,
    ) -> SessionEvent: ...

    def read_range(
        self, session_id: str, *, start_sequence: int = 1,
        end_sequence: int | None = None, limit: int = 10_000,
    ) -> tuple[SessionEvent, ...]: ...

    def inspect_integrity(
        self, session_id: str, *, start_sequence: int = 1,
        end_sequence: int | None = None, limit: int = 10_000,
    ) -> IntegrityReport: ...


@dataclass(frozen=True, slots=True)
class ModelVisibleRequest:
    """The complete request fact set visible to a model invocation.

    ``ModelRequest`` retains transport fields.  Tools and UI facts are kept
    beside it because they are session facts, not provider configuration.
    """

    request: ModelRequest
    request_id: str
    turn_id: str
    tools: tuple[Mapping[str, object], ...]
    ui_facts: Mapping[str, object]
    snapshot_digest: str


@dataclass(frozen=True, slots=True)
class DurableReplayResult:
    session_id: str
    replay: SessionReplay
    request: ModelVisibleRequest | None
    integrity: IntegrityReport
    crash_safe: bool
    recovered_sequence: int


def _text(payload: Mapping[str, object], name: str, *, required: bool = True) -> str:
    value = payload.get(name)
    if not required and value is None:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise IntegrityFailure(f"request snapshot field {name!r} is invalid")
    return value


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise IntegrityFailure(f"request snapshot field {name!r} must be an object")
    return MappingProxyType(dict(value))


def _tools(value: object) -> tuple[Mapping[str, object], ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise IntegrityFailure("request snapshot field 'tools' must be an array")
    if len(value) > 256:
        raise IntegrityFailure("request snapshot contains too many tools")
    return tuple(_mapping(item, "tools[]") for item in value)


def _canonical_digest(payload: Mapping[str, object]) -> str:
    try:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise IntegrityFailure("request snapshot is not canonical JSON") from exc
    return hashlib.sha256(encoded).hexdigest()


def reconstruct_model_visible_request(
    events: Sequence[DomainEvent], *, turn_id: str | None = None,
) -> ModelVisibleRequest | None:
    """Reconstruct the immutable request snapshot, including tools and UI facts."""
    selected: DomainEvent | None = None
    for event in events:
        if event.event_type not in {"model.requested", "prompt.snapshot"}:
            continue
        payload = event.payload
        candidate_turn = payload.get("turn_id", "")
        if turn_id is not None and candidate_turn != turn_id:
            continue
        if not isinstance(candidate_turn, str):
            raise IntegrityFailure("request snapshot turn_id is invalid")
        selected = event
    if selected is None:
        return None
    payload = selected.payload
    history = payload.get("history", ())
    options = payload.get("options", {})
    if not isinstance(history, (list, tuple)) or not isinstance(options, Mapping):
        raise IntegrityFailure("request snapshot has invalid history or options")
    stream = payload.get("stream", False)
    if not isinstance(stream, bool):
        raise IntegrityFailure("request snapshot stream is invalid")
    snapshot_digest = payload.get("snapshot_digest")
    calculated = _canonical_digest({key: value for key, value in payload.items()
                                    if key != "snapshot_digest"})
    if snapshot_digest is not None and snapshot_digest != calculated:
        raise IntegrityFailure("request snapshot digest mismatch")
    request = ModelRequest(
        prompt=_text(payload, "prompt"), tier=_text(payload, "tier"),
        system=_text(payload, "system", required=False), history=tuple(history),
        options=MappingProxyType(dict(options)), stream=stream,
    )
    return ModelVisibleRequest(
        request=request,
        request_id=_text(payload, "request_id", required=False),
        turn_id=candidate_turn,
        tools=_tools(payload.get("tools")),
        ui_facts=_mapping(payload.get("ui_facts", {}), "ui_facts"),
        snapshot_digest=calculated,
    )


def _domain_events(events: Sequence[SessionEvent]) -> tuple[DomainEvent, ...]:
    return tuple(DomainEvent(
        event_type=event.event_type, aggregate_type="session",
        aggregate_id=event.session_id, sequence=event.sequence,
        payload=dict(event.payload), id=event.event_id,
        created_at=event.occurred_at_utc,
    ) for event in events)


def crash_safe_replay(
    repository: AppendOnlySessionRepository, session_id: str, *,
    max_events: int = 10_000,
) -> DurableReplayResult:
    """Read, verify, and replay a committed session history.

    The full chain is read from sequence one so a crash-created gap or altered
    predecessor cannot be mistaken for a valid recoverable prefix.  The
    repository is never written, and no live configuration participates.
    """
    if not isinstance(session_id, str) or not session_id.strip():
        raise InvalidInput("session_id must be non-empty")
    if isinstance(max_events, bool) or not isinstance(max_events, int) or not 1 <= max_events <= 100_000:
        raise InvalidInput("max_events must be between 1 and 100000")
    # Adapters may impose a stricter local read ceiling.  Respect it while
    # retaining the caller's end-to-end bound; a full result smaller than that
    # ceiling still proves the tail was reached.
    adapter_limit = getattr(repository, "_max_read_limit", max_events)
    if isinstance(adapter_limit, bool) or not isinstance(adapter_limit, int) or adapter_limit < 1:
        adapter_limit = max_events
    read_limit = min(max_events, adapter_limit)
    events = repository.read_range(session_id, start_sequence=1, limit=read_limit)
    report = repository.inspect_integrity(session_id, start_sequence=1, limit=read_limit)
    if not report.valid:
        raise IntegrityFailure("session history failed integrity verification")
    if len(events) == read_limit:
        # A bounded read cannot prove that the tail was reached safely.
        raise IntegrityFailure("session history exceeds replay bound")
    domain = _domain_events(events)
    replay = replay_session(domain)
    request = reconstruct_model_visible_request(domain)
    return DurableReplayResult(session_id, replay, request, report, True,
                               events[-1].sequence if events else 0)


__all__ = [
    "AppendOnlySessionRepository", "ModelVisibleRequest", "DurableReplayResult",
    "reconstruct_model_visible_request", "crash_safe_replay",
]
