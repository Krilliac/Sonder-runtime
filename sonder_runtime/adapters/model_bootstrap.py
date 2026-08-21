"""Adapter from the transitional model runtime to typed bootstrap ports."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ..application.ports.model_bootstrap import LegacyModelRuntime, ModelBootstrapProvider
from ..application.ports.model_target import ModelTarget


@dataclass(frozen=True)
class LegacyModelBootstrapAdapter:
    """Expose only the model behavior required by application composition.

    The adapter receives a runtime object; it does not import or discover the
    historical root.  This keeps root loading and lifecycle policy in the
    explicit compatibility bootstrap while making the dependency replaceable
    in tests and in the next extraction slice.
    """

    runtime: LegacyModelRuntime

    def resolve_target(self, tier: str, strict: bool = False) -> ModelTarget:
        model, cloud, augment, tier_label = self.runtime._serve_target(tier, strict)
        return ModelTarget(model, cloud, tier_label, augment)

    def make_generate(
        self,
        model: str,
        system: str,
        temperature: float,
        num_predict: int,
        num_ctx: int,
        **kwargs: Any,
    ) -> Callable[..., str]:
        return self.runtime._make_generate(
            model,
            system,
            temperature,
            num_predict,
            num_ctx,
            **kwargs,
        )


def require_model_bootstrap_provider(
    provider: ModelBootstrapProvider,
) -> ModelBootstrapProvider:
    """Validate the callable surface at the composition boundary."""
    if not callable(getattr(provider, "resolve_target", None)):
        raise TypeError("model bootstrap provider must resolve targets")
    if not callable(getattr(provider, "make_generate", None)):
        raise TypeError("model bootstrap provider must construct generators")
    return provider


__all__ = ["LegacyModelBootstrapAdapter", "require_model_bootstrap_provider"]
