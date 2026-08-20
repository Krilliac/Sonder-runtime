"""WP5-AGENT-002 adapters for the Workbench and review modes.

The legacy modes have different side-effect envelopes, but they should enter
the unified agent registry through the same small registration contract.  This
module deliberately contains no model, persistence, or dispatch code.  A
registry only needs ``register(registration)`` and a caller consumes the
validated :class:`AgentInvocation` returned by the adapter.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


MAX_PROMPT_CHARS = 8_000
MAX_CONTEXT_CHARS = 16_000
MAX_OUTPUT_TOKENS = 8_192
MAX_STEPS = 12


class AgentRegistry(Protocol):
    """Minimal registration seam used by the adapter."""

    def register(self, registration: "AgentRegistration") -> Any: ...


@dataclass(frozen=True)
class AgentRegistration:
    """Provider-neutral metadata for one legacy mode."""

    name: str
    role: str
    mutation_policy: str
    default_tier: str = "code"
    max_steps: int = MAX_STEPS
    max_output_tokens: int = MAX_OUTPUT_TOKENS
    capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.role.strip():
            raise ValueError("agent name and role are required")
        if self.mutation_policy not in {"workspace", "read_only"}:
            raise ValueError("mutation_policy must be workspace or read_only")
        if self.max_steps <= 0 or self.max_steps > MAX_STEPS:
            raise ValueError("max_steps is outside the adapter bound")
        if self.max_output_tokens <= 0 or self.max_output_tokens > MAX_OUTPUT_TOKENS:
            raise ValueError("max_output_tokens is outside the adapter bound")


@dataclass(frozen=True)
class AgentInvocation:
    """A bounded request handed to the unified registry."""

    registration: AgentRegistration
    prompt: str
    context: str = ""
    correlation_id: str = ""
    metadata: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not self.prompt.strip():
            raise ValueError("prompt is required")
        if len(self.prompt) > MAX_PROMPT_CHARS:
            raise ValueError("prompt exceeds adapter bound")
        if len(self.context) > MAX_CONTEXT_CHARS:
            raise ValueError("context exceeds adapter bound")
        if not self.correlation_id.strip():
            raise ValueError("correlation_id is required")


class WorkbenchReviewAdapter:
    """Register and build bounded Workbench/review invocations.

    ``review`` is intentionally read-only.  The adapter does not infer or
    widen permissions from arbitrary metadata; the registry remains the owner
    of authorization and execution.
    """

    WORKBENCH = AgentRegistration(
        name="workbench",
        role="editor",
        mutation_policy="workspace",
        capabilities=("inspect", "implement", "validate"),
    )
    REVIEW = AgentRegistration(
        name="review",
        role="reviewer",
        mutation_policy="read_only",
        capabilities=("inspect", "validate", "report"),
    )

    def __init__(self) -> None:
        self._registrations = {item.name: item for item in (self.WORKBENCH, self.REVIEW)}

    @property
    def registrations(self) -> tuple[AgentRegistration, ...]:
        return tuple(self._registrations.values())

    def register(self, registry: AgentRegistry) -> tuple[AgentRegistration, ...]:
        """Install both registrations and return the installed definitions."""
        for registration in self.registrations:
            registry.register(registration)
        return self.registrations

    def invocation(
        self,
        name: str,
        prompt: str,
        *,
        correlation_id: str,
        context: str = "",
        metadata: dict[str, str] | None = None,
    ) -> AgentInvocation:
        """Create a bounded invocation for one registered legacy mode."""
        try:
            registration = self._registrations[name.strip().lower()]
        except (KeyError, AttributeError) as exc:
            raise ValueError("unknown agent mode: %s" % name) from exc
        pairs = tuple(sorted((str(key), str(value)) for key, value in (metadata or {}).items()))
        return AgentInvocation(
            registration=registration,
            prompt=prompt,
            context=context,
            correlation_id=correlation_id,
            metadata=pairs,
        )


__all__ = [
    "AgentInvocation",
    "AgentRegistration",
    "AgentRegistry",
    "WorkbenchReviewAdapter",
    "MAX_CONTEXT_CHARS",
    "MAX_OUTPUT_TOKENS",
    "MAX_PROMPT_CHARS",
    "MAX_STEPS",
]
