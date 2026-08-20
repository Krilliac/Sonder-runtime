"""Bounded Fleet/Autopilot adapter for the unified agent registry.

This module is intentionally a translation boundary.  Fleet and Autopilot
callers keep their existing request vocabulary, while execution, persistence,
and lifecycle ownership remain with the registry port supplied by the
composition root.  No mode-specific loop or store is opened here.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol


class AgentMode(str, Enum):
    FLEET = "fleet"
    AUTOPILOT = "autopilot"


class AgentRegistryError(ValueError):
    """Raised when a legacy-mode request cannot be translated safely."""


@dataclass(frozen=True)
class AgentLaunch:
    """Canonical launch envelope emitted by the adapter."""

    agent_id: str
    mode: AgentMode
    operation_id: str
    prompt: str
    parent_id: str | None = None
    idempotency_key: str = ""
    metadata: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        for name in ("agent_id", "operation_id", "prompt", "idempotency_key"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise AgentRegistryError(f"{name} must be non-empty")
        if self.parent_id is not None and not self.parent_id.strip():
            raise AgentRegistryError("parent_id must be non-empty when supplied")


class AgentRegistryPort(Protocol):
    """The shared registry capability consumed by both legacy modes."""

    def create(self, launch: AgentLaunch) -> Any: ...

    def resume(self, agent_id: str) -> Any: ...

    def cancel(self, agent_id: str, *, reason: str = "") -> Any: ...

    def stop(self, agent_id: str, *, reason: str = "") -> Any: ...

    def status(self, agent_id: str) -> Any: ...


class FleetAutopilotAdapter:
    """Translate Fleet and Autopilot requests into one registry contract.

    The adapter deliberately exposes only operations that both modes can
    safely share at this migration stage.  Unsupported mode-specific behavior
    stays in the owning mode until a later contract adds it explicitly.
    """

    def __init__(self, registry: AgentRegistryPort) -> None:
        self._registry = registry

    def launch_fleet(
        self,
        *,
        agent_id: str,
        operation_id: str,
        prompt: str,
        idempotency_key: str,
        parent_id: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> Any:
        return self._launch(
            AgentMode.FLEET, agent_id, operation_id, prompt, idempotency_key,
            parent_id, metadata,
        )

    def launch_autopilot(
        self,
        *,
        agent_id: str,
        operation_id: str,
        prompt: str,
        idempotency_key: str,
        parent_id: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> Any:
        return self._launch(
            AgentMode.AUTOPILOT, agent_id, operation_id, prompt,
            idempotency_key, parent_id, metadata,
        )

    def resume(self, agent_id: str) -> Any:
        return self._registry.resume(self._require_id(agent_id, "agent_id"))

    def cancel(self, agent_id: str, *, reason: str = "") -> Any:
        return self._registry.cancel(
            self._require_id(agent_id, "agent_id"), reason=reason,
        )

    def stop(self, agent_id: str, *, reason: str = "") -> Any:
        return self._registry.stop(
            self._require_id(agent_id, "agent_id"), reason=reason,
        )

    def status(self, agent_id: str) -> Any:
        return self._registry.status(self._require_id(agent_id, "agent_id"))

    def _launch(
        self,
        mode: AgentMode,
        agent_id: str,
        operation_id: str,
        prompt: str,
        idempotency_key: str,
        parent_id: str | None,
        metadata: dict[str, str] | None,
    ) -> Any:
        launch = AgentLaunch(
            agent_id=self._require_id(agent_id, "agent_id"),
            mode=mode,
            operation_id=self._require_id(operation_id, "operation_id"),
            prompt=self._require_id(prompt, "prompt"),
            parent_id=parent_id,
            idempotency_key=self._require_id(idempotency_key, "idempotency_key"),
            metadata=self._metadata(metadata),
        )
        return self._registry.create(launch)

    @staticmethod
    def _require_id(value: str, name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise AgentRegistryError(f"{name} must be non-empty")
        return value.strip()

    @staticmethod
    def _metadata(metadata: dict[str, str] | None) -> tuple[tuple[str, str], ...]:
        if metadata is None:
            return ()
        if not isinstance(metadata, dict):
            raise AgentRegistryError("metadata must be a string mapping")
        pairs = []
        for key, value in metadata.items():
            if not isinstance(key, str) or not key.strip() or not isinstance(value, str):
                raise AgentRegistryError("metadata keys and values must be strings")
            pairs.append((key.strip(), value))
        return tuple(sorted(pairs))


__all__ = ["AgentLaunch", "AgentMode", "AgentRegistryError", "AgentRegistryPort", "FleetAutopilotAdapter"]
