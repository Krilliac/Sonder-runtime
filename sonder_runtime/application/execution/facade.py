"""Provider-neutral execution graph for the production boundary.

The graph owns the identity shared by execution surfaces.  It does not own a
provider process: a controller and spill adapter are explicit capabilities,
so an unconfigured runtime can describe execution without accidentally
executing it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .containment import ContainmentDecision, GuardedContainerContract

logger = logging.getLogger(__name__)
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
        logger.debug(f"ExecutionApplicationFacade.local: world_id={world_id!r}, has_controller={controller is not None}")
        logger.info(f"local execution world created: world_id={world_id!r}, executable={controller is not None}")

        if controller is None:
            logger.warning(f"local execution world created without controller (not executable): world_id={world_id!r}")
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
        logger.debug(f"ExecutionApplicationFacade.guarded_container: world_id={world_id!r}, image_digest={'set' if image_digest else 'none'}, has_controller={controller is not None}")
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
        if not decision.accepted:
            logger.error(f"container admission rejected: world_id={world_id!r}, reason={decision.reason!r}")
            logger.warning(f"container admission rejected: world_id={world_id!r}, reason={decision.reason!r}, isolation={decision.isolation.truth.value!r}")
        elif decision.isolation.truth is IsolationTruth.UNVERIFIED:
            logger.warning(f"container admitted with unverified isolation: world_id={world_id!r}")
        logger.info(f"guarded container assessed: world_id={world_id!r}, containment={decision.status.value!r}, isolation={decision.isolation.truth.value!r}, executable={admitted is not None}")
        logger.debug(f"ExecutionApplicationFacade.guarded_container: containment_status={decision.status.value!r}, admitted={admitted is not None}, isolation_truth={decision.isolation.truth.value!r}")
        return cls(ExecutionGraph(world, admitted, output or BoundedOutputBuffer(), decision, spill))


__all__ = ["ExecutionApplicationFacade", "ExecutionGraph"]
