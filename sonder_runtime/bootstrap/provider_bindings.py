"""Compatibility imports for provider-binding ownership in adapters."""
from ..adapters.provider_bindings import (
    PROVIDER_TIERS,
    TIER_PROVIDER_ENV,
    ProviderBindings,
    normalize_provider,
    provider_bindings_from_env,
)

__all__ = [
    "PROVIDER_TIERS",
    "TIER_PROVIDER_ENV",
    "ProviderBindings",
    "normalize_provider",
    "provider_bindings_from_env",
]
