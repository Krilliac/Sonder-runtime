"""WP3-SEAM-007 application port for immutable session-history compaction.

The port carries value snapshots only.  It does not load, rewrite, delete, or
append session events; a repository adapter may later persist the returned
append-only event.  Keeping that ownership outside this module makes
re-compaction from the original history possible.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Protocol


class CompactionValidationError(ValueError):
    """Raised when a compaction request or result violates the port contract."""


def _freeze(value: Any) -> Any:
    """Recursively copy common JSON-shaped values into immutable values."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class SessionHistoryEvent:
    """An immutable event snapshot that can be used as compaction input."""

    event_id: str
    session_id: str
    sequence: int
    event_type: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    modality: str = "text"

    def __post_init__(self) -> None:
        if not self.event_id.strip() or not self.session_id.strip():
            raise CompactionValidationError("event_id and session_id are required")
        if not isinstance(self.sequence, int) or isinstance(self.sequence, bool) or self.sequence < 0:
            raise CompactionValidationError("event sequence must be a non-negative integer")
        if not self.event_type.strip() or not self.modality.strip():
            raise CompactionValidationError("event_type and modality are required")
        if not isinstance(self.payload, Mapping):
            raise TypeError("event payload must be a mapping")
        object.__setattr__(self, "payload", _freeze(dict(self.payload)))


@dataclass(frozen=True)
class SourceRange:
    """Inclusive identity range of the exact source events being summarized."""

    session_id: str
    start_sequence: int
    end_sequence: int
    start_event_id: str
    end_event_id: str

    def __post_init__(self) -> None:
        if not self.session_id.strip():
            raise CompactionValidationError("source range session_id is required")
        if not all(isinstance(value, int) and not isinstance(value, bool)
                   for value in (self.start_sequence, self.end_sequence)):
            raise CompactionValidationError("source range sequences must be integers")
        if self.start_sequence < 0 or self.end_sequence < self.start_sequence:
            raise CompactionValidationError("source range must be non-empty and ordered")
        if not self.start_event_id.strip() or not self.end_event_id.strip():
            raise CompactionValidationError("source range event identities are required")

    @property
    def start(self) -> int:
        """Compatibility-friendly alias for the inclusive start sequence."""
        return self.start_sequence

    @property
    def end(self) -> int:
        """Compatibility-friendly alias for the inclusive end sequence."""
        return self.end_sequence


@dataclass(frozen=True)
class CompactionSummary:
    """Structured retention output; modalities remain separately typed."""

    facts: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()
    unresolved_tasks: tuple[str, ...] = ()
    artifacts: tuple[str, ...] = ()
    tool_outcomes: tuple[str, ...] = ()
    modalities: tuple[SessionHistoryEvent, ...] = ()
    confidence: float | None = None

    def __post_init__(self) -> None:
        for name in ("facts", "decisions", "unresolved_tasks", "artifacts", "tool_outcomes"):
            values = tuple(getattr(self, name))
            if any(not isinstance(value, str) or not value.strip() for value in values):
                raise CompactionValidationError(f"summary {name} must contain non-empty strings")
            object.__setattr__(self, name, values)
        modalities = tuple(self.modalities)
        if any(not isinstance(value, SessionHistoryEvent) for value in modalities):
            raise TypeError("summary modalities must contain SessionHistoryEvent values")
        object.__setattr__(self, "modalities", modalities)
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise CompactionValidationError("summary confidence must be between 0 and 1")


@dataclass(frozen=True)
class CompactionEvent:
    """The event a caller may append; it never replaces source history."""

    event_id: str
    session_id: str
    source_range: SourceRange
    summary: CompactionSummary
    event_type: str = "compaction.completed"

    def __post_init__(self) -> None:
        if not self.event_id.strip() or not self.session_id.strip():
            raise CompactionValidationError("compaction event identity is required")
        if self.source_range.session_id != self.session_id:
            raise CompactionValidationError("compaction event and source range sessions differ")
        if self.event_type != "compaction.completed":
            raise CompactionValidationError("compaction event_type must be compaction.completed")


@dataclass(frozen=True)
class CompactionRequest:
    """Immutable request over a copied session-history snapshot."""

    session_id: str
    history: tuple[SessionHistoryEvent, ...]
    source_range: SourceRange
    max_summary_tokens: int | None = None

    def __post_init__(self) -> None:
        if not self.session_id.strip():
            raise CompactionValidationError("session_id is required")
        history = tuple(self.history)
        if not history:
            raise CompactionValidationError("history must not be empty")
        if any(not isinstance(event, SessionHistoryEvent) for event in history):
            raise TypeError("history must contain SessionHistoryEvent values")
        if any(event.session_id != self.session_id for event in history):
            raise CompactionValidationError("history contains a different session")
        if any(left.sequence >= right.sequence for left, right in zip(history, history[1:])):
            raise CompactionValidationError("history must be ordered by unique sequence")
        if len({event.event_id for event in history}) != len(history):
            raise CompactionValidationError("history event identities must be unique")
        if self.source_range.session_id != self.session_id:
            raise CompactionValidationError("source range belongs to a different session")
        if self.max_summary_tokens is not None and (
            not isinstance(self.max_summary_tokens, int)
            or isinstance(self.max_summary_tokens, bool)
            or self.max_summary_tokens <= 0
        ):
            raise CompactionValidationError("max_summary_tokens must be a positive integer")
        object.__setattr__(self, "history", history)
        _validate_source_range(history, self.source_range)


@dataclass(frozen=True)
class CompactionValidation:
    """Side-effect-free factual-retention assessment for a result."""

    valid: bool
    retained_facts: tuple[str, ...] = ()
    missing_facts: tuple[str, ...] = ()
    detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "retained_facts", tuple(self.retained_facts))
        object.__setattr__(self, "missing_facts", tuple(self.missing_facts))


@dataclass(frozen=True)
class CompactionResult:
    """Immutable result containing an append-only event and its validation."""

    session_id: str
    source_range: SourceRange
    summary: CompactionSummary
    appended_event: CompactionEvent
    validation: CompactionValidation

    def __post_init__(self) -> None:
        if self.session_id != self.source_range.session_id:
            raise CompactionValidationError("result and source range sessions differ")
        if self.appended_event.session_id != self.session_id:
            raise CompactionValidationError("result and appended event sessions differ")
        if self.appended_event.source_range != self.source_range:
            raise CompactionValidationError("appended event must bind the result source range")
        if self.appended_event.summary != self.summary:
            raise CompactionValidationError("appended event must contain the result summary")


def _validate_source_range(
    history: tuple[SessionHistoryEvent, ...], source_range: SourceRange
) -> None:
    selected = tuple(event for event in history
                     if source_range.start_sequence <= event.sequence <= source_range.end_sequence)
    expected_count = source_range.end_sequence - source_range.start_sequence + 1
    if len(selected) != expected_count:
        raise CompactionValidationError("source range must cover contiguous history events")
    if selected[0].sequence != source_range.start_sequence or selected[-1].sequence != source_range.end_sequence:
        raise CompactionValidationError("source range is outside the supplied history")
    if selected[0].event_id != source_range.start_event_id or selected[-1].event_id != source_range.end_event_id:
        raise CompactionValidationError("source range identities do not match history")


def validate_compaction_result(
    request: CompactionRequest, result: CompactionResult
) -> CompactionValidation:
    """Validate result identity and source binding without changing either value."""
    if result.session_id != request.session_id or result.source_range != request.source_range:
        raise CompactionValidationError("result does not match request session or source range")
    if result.appended_event.event_id in {event.event_id for event in request.history}:
        raise CompactionValidationError("compaction event identity collides with source history")
    return result.validation


class CompactionEngine(Protocol):
    """Application port implemented by a summarization/validation adapter."""

    # [any thread, async safe] Must not mutate or delete request history.
    def compact(self, request: CompactionRequest) -> CompactionResult: ...

    # [any thread, thread-safe] Pure factual-retention validation.
    def validate(
        self, request: CompactionRequest, result: CompactionResult
    ) -> CompactionValidation: ...


__all__ = [
    "CompactionEngine", "CompactionEvent", "CompactionRequest", "CompactionResult",
    "CompactionSummary", "CompactionValidation", "CompactionValidationError",
    "SessionHistoryEvent", "SourceRange", "validate_compaction_result",
]
