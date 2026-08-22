"""Application façade for caller-local scoped provider policies."""
from __future__ import annotations

from dataclasses import dataclass

from ..domain.provider_override_policy import ProviderOverridePolicy


@dataclass(frozen=True)
class ProviderOverrideService:
    """Apply policy changes without owning or mutating provider lifecycles."""

    policy: ProviderOverridePolicy

    def replace(self, scope: str, provider: str, replacement: str) -> "ProviderOverrideService":
        return type(self)(self.policy.with_override(scope, provider, replacement))

    def remove(self, scope: str, provider: str) -> "ProviderOverrideService":
        return type(self)(self.policy.without_override(scope, provider))

    def resolve(self, provider: str, scopes: object = None) -> str:
        return self.policy.resolve(provider, scopes)


__all__ = ["ProviderOverrideService"]
