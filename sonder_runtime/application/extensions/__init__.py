"""Application services for extension admission and quarantine."""

from .quarantine import QuarantineDecision, QuarantineReason, QuarantineRegistry
from .facade import (
    ExtensionApplicationFacade,
    ExtensionAuthority,
    ExtensionAuthorityDenied,
    ExtensionFacadeError,
    ExtensionRegistryHealth,
)

__all__ = [
    "ExtensionApplicationFacade",
    "ExtensionAuthority",
    "ExtensionAuthorityDenied",
    "ExtensionFacadeError",
    "ExtensionRegistryHealth",
    "QuarantineDecision",
    "QuarantineReason",
    "QuarantineRegistry",
]
