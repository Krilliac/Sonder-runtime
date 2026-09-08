"""Canonical durable child records shared by storage ports and supervision."""

from dataclasses import dataclass
from .subagents import (
    InvalidSubagentRequest,
    SubagentRequest,
    SubagentStatus,
    SubagentUsage,
    SubagentResult,
)
from ..subagents.continuable import ContinuableCheckpoint


@dataclass(frozen=True, slots=True)
class ChildSessionLineage:
    """Immutable parent chain captured when the child is created."""

    parent_id: str
    ancestors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.parent_id.strip() or any(
            not item.strip() for item in self.ancestors
        ):
            raise InvalidSubagentRequest("child lineage ids must be non-empty")
        if self.parent_id in self.ancestors:
            raise InvalidSubagentRequest("child lineage contains a cycle")

    @property
    def chain(self) -> tuple[str, ...]:
        return self.ancestors + (self.parent_id,)


@dataclass(frozen=True, slots=True)
class DurableChildSession:
    request: SubagentRequest
    lineage: ChildSessionLineage
    status: SubagentStatus = SubagentStatus.CREATED
    checkpoint: ContinuableCheckpoint | None = None
    revision: int = 0
    usage: SubagentUsage = SubagentUsage()
    result: SubagentResult | None = None
    recovery_required: bool = False
    cancellation_requested: bool = False
    cancellation_reason: str | None = None
