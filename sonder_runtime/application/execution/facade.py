"""Provider-neutral execution graph for the production boundary.

The graph owns the identity shared by execution surfaces.  It does not own a
provider process: a controller and spill adapter are explicit capabilities,
so an unconfigured runtime can describe execution without accidentally
executing it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .containment import ContainmentDecision, GuardedContainerContract
from .world_control import (
    BoundedOutputBuffer,
    ExecutionSurface,
    ExecutionWorldController,
    IsolationClaim,
    ExecutionWorldKind,
    IsolationTruth,
    SharedExecutionWorld,
    WorldBinding,
    require_same_world,
)


@dataclass(frozen=True, slots=True)
class ExecutionGraph:
    """The explicit ownership graph for one execution world."""

    world: SharedExecutionWorld
    controller: ExecutionWorldController | None
    output: BoundedOutputBuffer
    containment: ContainmentDecision | None = None
    spill: Any | None = None

    def bind(self, surface: ExecutionSurface) -> WorldBinding:
        return self.world.bind(surface)

    def require_same_world(self, *bindings: WorldBinding) -> str:
        return require_same_world(*bindings)

    @property
    def executable(self) -> bool:
        """Whether an actual controller was explicitly supplied."""

        return self.controller is not None


class ExecutionApplicationFacade:
    """Small interface for callers that need the composed execution graph."""

    def __init__(
        self,
        graph: ExecutionGraph,
    ) -> None:
        if not isinstance(graph, ExecutionGraph):
            raise TypeError("graph must be an ExecutionGraph")
        self._graph = graph

    @property
    def graph(self) -> ExecutionGraph:
        return self._graph

    @property
    def world(self) -> SharedExecutionWorld:
        return self._graph.world

    @property
    def controller(self) -> ExecutionWorldController | None:
        return self._graph.controller

    @property
    def output(self) -> BoundedOutputBuffer:
        return self._graph.output

    @property
    def containment(self) -> ContainmentDecision | None:
        return self._graph.containment

    @property
    def spill(self) -> Any | None:
        return self._graph.spill

    @property
    def executable(self) -> bool:
        return self._graph.executable

    def bind(self, surface: ExecutionSurface) -> WorldBinding:
        return self._graph.bind(surface)

    def require_same_world(self, *bindings: WorldBinding) -> str:
        return self._graph.require_same_world(*bindings)

    @classmethod
    def local(
        cls,
        *,
        world_id: str = "local-execution",
        controller: ExecutionWorldController | None = None,
        output: BoundedOutputBuffer | None = None,
        spill: Any | None = None,
    ) -> "ExecutionApplicationFacade":
        """Build a local graph without implying a security boundary."""

        world = SharedExecutionWorld(
            world_id,
            ExecutionWorldKind.LOCAL,
            frozenset(ExecutionSurface),
            IsolationClaim(
                truth=IsolationTruth.UNVERIFIED,
                rationale="local execution has no verified security boundary",
            ),
            provider_id="local",
        )
        return cls(ExecutionGraph(world, controller, output or BoundedOutputBuffer(), spill=spill))

    @classmethod
    def guarded_container(
        cls,
        *,
        capability: Any,
        image_digest: str | None,
        world_id: str = "container-execution",
        controller: ExecutionWorldController | None = None,
        output: BoundedOutputBuffer | None = None,
        spill: Any | None = None,
    ) -> "ExecutionApplicationFacade":
        contract = GuardedContainerContract()
        decision = contract.assess(capability, image_digest=image_digest)
        world = SharedExecutionWorld(
            world_id,
            ExecutionWorldKind.CONTAINER,
            frozenset(ExecutionSurface),
            decision.isolation,
            provider_id=decision.provider_id or "container",
        )
        # Admission is separate from controller wiring; a rejected decision
        # never becomes executable merely because a controller was supplied.
        admitted = controller if decision.accepted else None
        return cls(ExecutionGraph(world, admitted, output or BoundedOutputBuffer(), decision, spill))


__all__ = ["ExecutionApplicationFacade", "ExecutionGraph"]
