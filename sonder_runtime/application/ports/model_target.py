"""Root-free model target and system-composition ports.

The legacy composition root historically supplied tier resolution, system
prompt composition, and generator construction through ``server`` helpers.
These contracts keep those decisions at the application boundary so model
adapters can be tested and assembled with provider-owned dependencies.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping, Protocol


@dataclass(frozen=True)
class ModelTarget:
    """One resolved model target returned by an application policy owner."""

    model: str | None
    cloud: bool
    tier_label: str | None
    augment_system: bool = True


class ModelTargetResolver(Protocol):
    """Resolve a user-facing tier without importing a composition root."""

    def __call__(self, tier: str, strict: bool = False) -> ModelTarget: ...


class ModelSystemBuilder(Protocol):
    """Compose request-scoped system text for an already resolved target."""

    def __call__(
        self,
        system: str,
        trace: bool,
        persona: str,
        *,
        model: str = "",
        cloud: bool = False,
    ) -> str: ...


class ModelGenerateFactory(Protocol):
    """Build a provider-owned prompt/history callable for one target."""

    def __call__(
        self,
        model: str,
        system: str,
        temperature: float,
        num_predict: int,
        num_ctx: int,
        *,
        cloud: bool = False,
        timeout: float | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ): ...


__all__ = [
    "ModelGenerateFactory",
    "ModelSystemBuilder",
    "ModelTarget",
    "ModelTargetResolver",
]
