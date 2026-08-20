"""Capability provider lifecycle and atomic registration."""

from .lifecycle import (
    CapabilityRegistry,
    ProviderLifecycleError,
    ProviderRegistration,
    RegistrationScope,
)

__all__ = [
    "CapabilityRegistry",
    "ProviderLifecycleError",
    "ProviderRegistration",
    "RegistrationScope",
]
