"""Append-only capture and deterministic replay for one model-visible turn.

The capture service is the write-side counterpart to ``SessionQueryEngine``.
It records the exact request snapshot first, then the user/tool/model facts
that belong to the turn.  The repository remains the source of truth; replay
and export are run against that same committed stream before the result is
returned to the caller.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from ...domain.common.errors import (
    Cancelled, CapacityExceeded, ConcurrencyConflict, Conflict,
    DeadlineExceeded, DependencyUnavailable, Forbidden, IntegrityFailure,
    InternalFailure, InvalidInput, MigrationRequired, NotFound, Unauthenticated,
)
from ..ports.model_gateway import ModelRequest
from ..ports.session_repository import SessionEvent, SessionRepository
from .durable_replay import DurableReplayResult, crash_safe_replay
from .query_export import SessionExport, SessionQueryEngine


_MAX_TOOLS = 256
_MAX_PAYLOAD_BYTES = 2_000_000
_FAILURE_CODES = frozenset({
    error.code for error in (
        Cancelled, CapacityExceeded, ConcurrencyConflict, Conflict,
        DeadlineExceeded, DependencyUnavailable, Forbidden, IntegrityFailure,
        InternalFailure, InvalidInput, MigrationRequired, NotFound, Unauthenticated,
    )
})


@dataclass(frozen=True, slots=True)
class CapturedRequest:
    """Committed admission for one internal, single-attempt model request."""

    session_id: str
    turn_id: str
    request_id: str
    appended: tuple[SessionEvent, ...]


@dataclass(frozen=True, slots=True)
class CapturedTool:
    """A bounded tool invocation and its consequential model-visible result."""

    call_id: str
    name: str
    arguments: Mapping[str, object]
    result: object


@dataclass(frozen=True, slots=True)
class CapturedTurn:
    """Evidence returned after a complete append/replay/export cycle."""

    session_id: str
    turn_id: str
    appended: tuple[SessionEvent, ...]
    replay: DurableReplayResult
    export: SessionExport


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidInput(f"{name} must be non-empty text")
    return value


def _json_copy(value: object, name: str) -> Any:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"))
        if len(encoded.encode("utf-8")) > _MAX_PAYLOAD_BYTES:
            raise InvalidInput(f"{name} exceeds the capture size bound")
        return json.loads(encoded)
    except InvalidInput:
        raise
    except (TypeError, ValueError) as exc:
        raise InvalidInput(f"{name} must be JSON-serializable") from exc


def _canonical_json(value: object, name: str) -> str:
    copied = _json_copy(value, name)
    return json.dumps(copied, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _snapshot_payload(
    request: ModelRequest,
    *,
    request_id: str,
    turn_id: str,
    tools: Sequence[Mapping[str, object]],
    ui_facts: Mapping[str, object],
) -> dict[str, object]:
    history = _json_copy(list(request.history), "request.history")
    options = _json_copy(dict(request.options), "request.options")
    tool_manifest = _json_copy([dict(tool) for tool in tools], "request.tools")
    ui = _json_copy(dict(ui_facts), "request.ui_facts")
    payload: dict[str, object] = {
        "request_id": request_id,
        "turn_id": turn_id,
        "prompt": _required_text(request.prompt, "request.prompt"),
        "tier": _required_text(request.tier, "request.tier"),
        "system": request.system,
        "history": history,
        "options": options,
        "stream": request.stream,
        "tools": tool_manifest,
        "ui_facts": ui,
    }
    if request.context_packet is not None and request.provenance is not None:
        # Only redacted provenance crosses the durable event boundary.
        from ..security.prompt_provenance import PromptProvenanceBoundary

        payload["provenance"] = PromptProvenanceBoundary.request_event_metadata(
            request.context_packet, request.provenance,
        )
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
    payload["snapshot_digest"] = hashlib.sha256(canonical).hexdigest()
    return payload


def _validate_tools(tools: Sequence[CapturedTool]) -> tuple[CapturedTool, ...]:
    values = tuple(tools)
    if len(values) > _MAX_TOOLS:
        raise InvalidInput(f"tools must contain at most {_MAX_TOOLS} entries")
    for index, tool in enumerate(values):
        if not isinstance(tool, CapturedTool):
            raise InvalidInput(f"tools[{index}] must be CapturedTool")
        _required_text(tool.call_id, f"tools[{index}].call_id")
        _required_text(tool.name, f"tools[{index}].name")
        if not isinstance(tool.arguments, Mapping):
            raise InvalidInput(f"tools[{index}].arguments must be an object")
        _json_copy(dict(tool.arguments), f"tools[{index}].arguments")
        _json_copy(tool.result, f"tools[{index}].result")
    return values


class SessionCaptureService:
    """Capture complete model-visible turns into an append-only repository."""

    def __init__(
        self,
        repository: SessionRepository,
        *,
        query_engine: SessionQueryEngine | None = None,
        replay_limit: int = 10_000,
    ) -> None:
        if not 1 <= replay_limit <= 100_000:
            raise InvalidInput("replay_limit must be between 1 and 100000")
        self._repository = repository
        self._query = query_engine or SessionQueryEngine(
            repository, max_page_size=min(100, replay_limit),
            max_scan=replay_limit,
        )
        self._replay_limit = replay_limit

    def replay(self, session_id: str) -> DurableReplayResult:
        """Replay one persisted session after a process/database restart.

        This is deliberately read-only and uses the same bounded,
        integrity-first path as ``capture_turn``.  A partial or altered
        append stream therefore fails closed instead of returning a prefix.
        """
        return crash_safe_replay(
            self._repository, session_id, max_events=self._replay_limit,
        )

    def capture_turn(
        self,
        session_id: str,
        turn_id: str,
        request: ModelRequest,
        *,
        request_id: str,
        tools: Sequence[CapturedTool] = (),
        ui_facts: Mapping[str, object] | None = None,
        user_message: str | None = None,
        model_response: str | None = None,
    ) -> CapturedTurn:
        """Append request, user/tool/model facts, then prove replay/export.

        The request snapshot is always the first event.  Every later event is
        optional and independently committed; if a process dies between
        appends, ``crash_safe_replay`` rejects an incomplete/tampered stream
        rather than inventing a successful turn.
        """
        session_id = _required_text(session_id, "session_id")
        turn_id = _required_text(turn_id, "turn_id")
        request_id = _required_text(request_id, "request_id")
        if not isinstance(request, ModelRequest):
            raise InvalidInput("request must be ModelRequest")
        if ui_facts is None:
            ui_facts = {}
        if not isinstance(ui_facts, Mapping):
            raise InvalidInput("ui_facts must be an object")
        ui_copy = _json_copy(dict(ui_facts), "ui_facts")
        values = _validate_tools(tools)
        manifest = tuple({"name": tool.name, "call_id": tool.call_id}
                         for tool in values)
        payload = _snapshot_payload(
            request, request_id=request_id, turn_id=turn_id,
            tools=manifest, ui_facts=ui_copy,
        )
        appended: list[SessionEvent] = [self._repository.append(
            session_id, "model.requested", payload,
        )]
        if user_message is not None:
            _required_text(user_message, "user_message")
            appended.append(self._repository.append(
                session_id, "user.message",
                {"content": user_message, "turn_id": turn_id},
            ))
        for tool in values:
            arguments = _json_copy(dict(tool.arguments), "tool.arguments")
            appended.append(self._repository.append(
                session_id, "tool.call",
                {"content": _canonical_json(arguments, "tool.arguments"),
                 "turn_id": turn_id, "call_id": tool.call_id, "name": tool.name},
            ))
            result = _json_copy(tool.result, "tool.result")
            appended.append(self._repository.append(
                session_id, "tool.result",
                {"content": _canonical_json(result, "tool.result"),
                 "result": result,
                 "turn_id": turn_id, "call_id": tool.call_id, "name": tool.name},
            ))
        if model_response is not None:
            _required_text(model_response, "model_response")
            appended.append(self._repository.append(
                session_id, "model.response",
                {"content": model_response, "turn_id": turn_id,
                 "request_id": request_id},
            ))
        return self._finalize_capture(session_id, turn_id, tuple(appended))

    def begin_request(
        self,
        session_id: str,
        turn_id: str,
        request: ModelRequest,
        *,
        request_id: str,
        user_message: str | None = None,
    ) -> CapturedRequest:
        """Commit request intent and optional input before model dispatch.

        Validate admission before the first write. If a later append fails,
        preserve the committed prefix as evidence of unresolved admission.
        """
        session_id = _required_text(session_id, "session_id")
        turn_id = _required_text(turn_id, "turn_id")
        request_id = _required_text(request_id, "request_id")
        if not isinstance(request, ModelRequest):
            raise InvalidInput("request must be ModelRequest")
        if user_message is not None:
            _required_text(user_message, "user_message")
        payload = _snapshot_payload(
            request, request_id=request_id, turn_id=turn_id, tools=(), ui_facts={},
        )
        appended = [self._repository.append(session_id, "model.requested", payload)]
        if user_message is not None:
            appended.append(self._repository.append(
                session_id, "user.message",
                {"content": user_message, "turn_id": turn_id, "request_id": request_id},
            ))
        return CapturedRequest(session_id, turn_id, request_id, tuple(appended))

    def complete_request(
        self, pending: CapturedRequest, *, model_response: str,
    ) -> CapturedTurn:
        """Commit the successful output for this admitted model attempt."""
        _required_text(model_response, "model_response")
        event = self._repository.append(
            pending.session_id, "model.response",
            {"content": model_response, "turn_id": pending.turn_id,
             "request_id": pending.request_id},
        )
        return self._finalize_capture(
            pending.session_id, pending.turn_id, (*pending.appended, event),
        )

    def fail_request(
        self, pending: CapturedRequest, *, error_code: str,
    ) -> SessionEvent:
        """Commit only correlation identities and an allowlisted failure code."""
        if not isinstance(error_code, str) or error_code not in _FAILURE_CODES:
            raise InvalidInput("error_code must be a known domain error code")
        return self._repository.append(
            pending.session_id, "model.failed",
            {"turn_id": pending.turn_id, "request_id": pending.request_id,
             "error_code": error_code},
        )

    def _finalize_capture(
        self, session_id: str, turn_id: str, appended: tuple[SessionEvent, ...],
    ) -> CapturedTurn:
        """Verify the committed stream using the existing replay/export bounds."""
        try:
            replay = crash_safe_replay(
                self._repository, session_id, max_events=self._replay_limit,
            )
            exported = self._query.export_events(
                session_id, max_events=self._replay_limit,
            )
        except Exception as exc:
            if isinstance(exc, (InvalidInput, IntegrityFailure)):
                raise
            raise IntegrityFailure("captured session could not be replayed/exported") from exc
        if replay.request is None or replay.request.turn_id != turn_id:
            raise IntegrityFailure("captured request is missing from replay")
        if exported.truncated:
            raise IntegrityFailure("captured session export did not reach its tail")
        return CapturedTurn(session_id, turn_id, tuple(appended), replay, exported)


__all__ = ["CapturedRequest", "CapturedTool", "CapturedTurn", "SessionCaptureService"]
