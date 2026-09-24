"""Typed admission boundary between ordinary chat and execution lanes.

Conversation remains a chat-lane operation. A separate execution handoff is
created only when the caller has already classified an eligible work request;
dispatch, authorization, and durable work receipts remain at the transport
edge and its existing lane services.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

_CHAT_LANE = "chat"
_EXECUTION_MODES = frozenset({"workbench", "autopilot", "fleet", "decide"})
_MAX_OBJECTIVE_CHARS = 12_000
_MAX_PROJECT_CHARS = 512
_MAX_ITEMS = 8
_MAX_ITEM_CHARS = 240


def _bounded_text(value: object, name: str, maximum: int, *, exact: bool = False) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must be non-empty bounded text")
    return value if exact else value.strip()


def _bounded_items(values: object, name: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise ValueError(f"{name} must be a bounded sequence")  # noqa: TRY004 - preserve the DTO validation ValueError contract
    if len(values) > _MAX_ITEMS:
        raise ValueError(f"{name} exceeds {_MAX_ITEMS} items")
    return tuple(_bounded_text(value, name, _MAX_ITEM_CHARS) for value in values)


@dataclass(frozen=True, slots=True)
class ChatHandoffProvenance:
    """Non-prompt-visible provenance for an execution handoff."""

    surface: str
    reason: str
    correlation_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "surface", _bounded_text(self.surface, "provenance.surface", 80))
        object.__setattr__(self, "reason", _bounded_text(self.reason, "provenance.reason", _MAX_ITEM_CHARS))
        if not isinstance(self.correlation_id, str):
            raise ValueError("provenance.correlation_id must be text")  # noqa: TRY004 - preserve the DTO validation ValueError contract
        if self.correlation_id:
            object.__setattr__(
                self, "correlation_id",
                _bounded_text(self.correlation_id, "provenance.correlation_id", 160),
            )


@dataclass(frozen=True, slots=True)
class ChatHandoff:
    """Bounded, exact work intent handed from chat to an existing work lane."""

    objective: str
    requested_mode: str
    project: str
    constraints: tuple[str, ...] = ()
    durable_context_refs: tuple[str, ...] = ()
    success_criteria: tuple[str, ...] = ()
    provenance: ChatHandoffProvenance | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "objective", _bounded_text(self.objective, "objective", _MAX_OBJECTIVE_CHARS, exact=True),
        )
        if not isinstance(self.requested_mode, str) or self.requested_mode not in _EXECUTION_MODES:
            raise ValueError("requested_mode must be a known execution mode")
        if not isinstance(self.project, str) or len(self.project) > _MAX_PROJECT_CHARS:
            raise ValueError("project exceeds the bounded handoff limit")
        object.__setattr__(self, "constraints", _bounded_items(self.constraints, "constraints"))
        object.__setattr__(
            self, "durable_context_refs",
            _bounded_items(self.durable_context_refs, "durable_context_refs"),
        )
        object.__setattr__(
            self, "success_criteria", _bounded_items(self.success_criteria, "success_criteria"),
        )
        if type(self.provenance) is not ChatHandoffProvenance:
            raise ValueError("handoff provenance is required")

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-safe handoff projection for edge receipts."""
        return {
            "objective": self.objective,
            "requested_mode": self.requested_mode,
            "project": self.project,
            "constraints": list(self.constraints),
            "durable_context_refs": list(self.durable_context_refs),
            "success_criteria": list(self.success_criteria),
            "provenance": {
                "surface": self.provenance.surface,
                "reason": self.provenance.reason,
                "correlation_id": self.provenance.correlation_id,
            },
        }


@dataclass(frozen=True, slots=True)
class ChatLaneDecision:
    """A non-mutating chat decision or one eligible execution handoff."""

    lane: str
    reason: str
    handoff: ChatHandoff | None = None
    plan_only: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.lane, str) or (self.lane != _CHAT_LANE and self.lane not in _EXECUTION_MODES):
            raise ValueError("lane must be chat or a known execution mode")
        object.__setattr__(self, "reason", _bounded_text(self.reason, "reason", _MAX_ITEM_CHARS))
        if not isinstance(self.plan_only, bool):
            raise ValueError("plan_only must be a boolean")  # noqa: TRY004 - preserve the DTO validation ValueError contract
        if self.lane == _CHAT_LANE and (self.handoff is not None or self.plan_only):
            raise ValueError("ordinary chat cannot carry an execution handoff")
        if self.lane != _CHAT_LANE and type(self.handoff) is not ChatHandoff:
            raise ValueError("execution lane requires a handoff")

    @property
    def is_execution(self) -> bool:
        return self.handoff is not None


class ChatLaneService:
    """Classify only the chat-to-work seam; it never authorizes or dispatches."""

    def __init__(self, classify_execution: Callable[[str], Mapping[str, object] | None]) -> None:
        self._classify_execution = classify_execution

    def decide(
        self,
        objective: str,
        *,
        project: str = "",
        constraints: Sequence[object] = (),
        durable_context_refs: Sequence[object] = (),
        success_criteria: Sequence[object] = (),
        provenance: ChatHandoffProvenance,
        intent_override: Mapping[str, object] | None = None,
    ) -> ChatLaneDecision:
        """Keep ordinary conversation in chat; package eligible work exactly once."""
        if not isinstance(objective, str):
            raise ValueError("objective must be text")  # noqa: TRY004 - preserve the DTO validation ValueError contract
        intent = intent_override if intent_override is not None else self._classify_execution(objective)
        if not intent:
            # A regular chat request is not a handoff object and intentionally
            # keeps its own surface limits; this seam must not reject it.
            return ChatLaneDecision(_CHAT_LANE, "ordinary conversation")
        mode = intent.get("mode") if isinstance(intent, Mapping) else None
        reason = intent.get("reason") if isinstance(intent, Mapping) else None
        plan_only = intent.get("plan_only", False) if isinstance(intent, Mapping) else False
        if (mode not in _EXECUTION_MODES or not isinstance(reason, str) or not reason.strip()
                or not isinstance(plan_only, bool)):
            raise ValueError("execution classifier returned an invalid decision")
        return ChatLaneDecision(
            lane=mode,
            reason=reason.strip()[:_MAX_ITEM_CHARS],
            plan_only=plan_only,
            handoff=ChatHandoff(
                objective=objective,
                requested_mode=mode,
                project=project,
                constraints=constraints,
                durable_context_refs=durable_context_refs,
                success_criteria=success_criteria,
                provenance=provenance,
            ),
        )
