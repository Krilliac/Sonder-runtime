"""Application port for bounded child-agent execution (WP3 SEAM-009).

This module defines the provider-neutral contract only. A provider owns child
execution and its resources; callers own request context and consume immutable
snapshots/results. No fleet, workbench, model, or persistence adapter is
referenced here.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from typing import Protocol

from ..context import OperationContext


class SubagentStatus(str, Enum):
    CREATED = "created"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


TERMINAL_SUBAGENT_STATUSES = frozenset({
    SubagentStatus.SUCCEEDED,
    SubagentStatus.FAILED,
    SubagentStatus.CANCELLED,
    SubagentStatus.TIMED_OUT,
})


class SubagentProtocolError(RuntimeError):
    """Raised when a provider violates the application port contract."""


class InvalidSubagentRequest(ValueError):
    """Raised when a caller supplies an invalid child request or budget."""


@dataclass(frozen=True)
class SubagentBudget:
    """Hard ceilings inherited by a child and never widened by a provider."""

    max_children: int | None = None
    max_depth: int | None = None
    max_concurrency: int | None = None
    max_steps: int | None = None
    max_wall_seconds: float | None = None
    max_output_tokens: int | None = None

    def __post_init__(self) -> None:
        values = {
            "max_children": self.max_children,
            "max_depth": self.max_depth,
            "max_concurrency": self.max_concurrency,
            "max_steps": self.max_steps,
            "max_wall_seconds": self.max_wall_seconds,
            "max_output_tokens": self.max_output_tokens,
        }
        if all(value is None for value in values.values()):
            raise InvalidSubagentRequest("subagent budget must have a ceiling")
        for name, value in values.items():
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise InvalidSubagentRequest(f"{name} must be a positive number")
            if value <= 0 or not math.isfinite(float(value)):
                raise InvalidSubagentRequest(f"{name} must be a positive finite number")
            if name != "max_wall_seconds" and not isinstance(value, int):
                raise InvalidSubagentRequest(f"{name} must be an integer")


@dataclass(frozen=True)
class SubagentRequest:
    """Input needed to create one child, including its explicit parent."""

    parent_id: str
    prompt: str
    budget: SubagentBudget
    child_id: str | None = None
    metadata: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.parent_id, str) or not self.parent_id.strip():
            raise InvalidSubagentRequest("parent_id must be non-empty")
        if not isinstance(self.prompt, str) or not self.prompt.strip():
            raise InvalidSubagentRequest("prompt must be non-empty")
        if self.child_id is not None and (
            not isinstance(self.child_id, str) or not self.child_id.strip()
        ):
            raise InvalidSubagentRequest("child_id must be non-empty when supplied")


@dataclass(frozen=True)
class SubagentUsage:
    steps: int = 0
    output_tokens: int | None = None
    wall_seconds: float | None = None

    def __post_init__(self) -> None:
        if isinstance(self.steps, bool) or not isinstance(self.steps, int) or self.steps < 0:
            raise SubagentProtocolError("subagent steps must be a non-negative integer")
        if self.output_tokens is not None and (
            isinstance(self.output_tokens, bool)
            or not isinstance(self.output_tokens, int)
            or self.output_tokens < 0
        ):
            raise SubagentProtocolError("subagent output_tokens must be non-negative")
        if self.wall_seconds is not None and (
            self.wall_seconds < 0 or not math.isfinite(float(self.wall_seconds))
        ):
            raise SubagentProtocolError("subagent wall_seconds must be finite and non-negative")


@dataclass(frozen=True)
class SubagentError:
    code: str
    message: str
    retryable: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not self.code.strip() or not isinstance(self.message, str) or not self.message.strip():
            raise SubagentProtocolError("subagent errors require code and message")


@dataclass(frozen=True)
class SubagentSnapshot:
    child_id: str
    parent_id: str
    status: SubagentStatus
    budget: SubagentBudget
    usage: SubagentUsage = SubagentUsage()
    cancellation_reason: str | None = None


@dataclass(frozen=True)
class SubagentResult:
    """The single terminal envelope returned for every child."""

    child_id: str
    parent_id: str
    status: SubagentStatus
    output: str = ""
    error: SubagentError | None = None
    usage: SubagentUsage = SubagentUsage()

    def __post_init__(self) -> None:
        if self.status not in TERMINAL_SUBAGENT_STATUSES:
            raise SubagentProtocolError("subagent result must have a terminal status")
        if not isinstance(self.child_id, str) or not self.child_id.strip() or not isinstance(self.parent_id, str) or not self.parent_id.strip():
            raise SubagentProtocolError("subagent result requires child and parent ids")
        if not isinstance(self.output, str):
            raise SubagentProtocolError("subagent output must be text")
        if self.status is SubagentStatus.SUCCEEDED and self.error is not None:
            raise SubagentProtocolError("successful subagent result cannot contain an error")
        if self.status is not SubagentStatus.SUCCEEDED and self.error is None:
            raise SubagentProtocolError("non-successful subagent result requires an error")


class SubagentHandle(Protocol):
    """Non-owning capability for one provider-owned child."""

    @property
    def child_id(self) -> str: ...

    @property
    def parent_id(self) -> str: ...

    # [any thread, thread-safe] First reason wins; cancellation is cooperative.
    def cancel(self, *, reason: str = "cancellation requested") -> bool: ...

    # [any thread, thread-safe] Waits for this child to reach a terminal state.
    def result(self, timeout: float | None = None) -> SubagentResult: ...

    # [any thread, thread-safe] Returns an immutable point-in-time view.
    def snapshot(self) -> SubagentSnapshot: ...


class SubagentProvider(Protocol):
    """Spawn and supervise bounded children under explicit parent linkage."""

    # [any thread, async safe] The provider allocates the child id if omitted.
    def spawn(self, request: SubagentRequest, context: OperationContext) -> SubagentHandle: ...

    # [any thread, thread-safe] Snapshot lookup; unknown ids are rejected.
    def snapshot(self, child_id: str) -> SubagentSnapshot: ...

    # [any thread, thread-safe] Request cancellation and return whether new.
    def cancel(self, child_id: str, *, reason: str = "cancellation requested") -> bool: ...

    # [any thread, thread-safe] Wait for provider-owned children to quiesce.
    def close(self, timeout: float | None = None) -> bool: ...


def validate_child_budget(child: SubagentBudget, parent: SubagentBudget) -> None:
    """Ensure a child cannot widen a finite parent ceiling."""
    for name in (
        "max_children", "max_depth", "max_concurrency", "max_steps",
        "max_wall_seconds", "max_output_tokens",
    ):
        parent_value = getattr(parent, name)
        child_value = getattr(child, name)
        if parent_value is not None and (child_value is None or child_value > parent_value):
            raise InvalidSubagentRequest(f"child budget widens parent {name}")


__all__ = [
    "InvalidSubagentRequest", "SubagentBudget", "SubagentError", "SubagentHandle",
    "SubagentProtocolError", "SubagentProvider", "SubagentRequest", "SubagentResult",
    "SubagentSnapshot", "SubagentStatus", "SubagentUsage",
    "TERMINAL_SUBAGENT_STATUSES", "validate_child_budget",
]
