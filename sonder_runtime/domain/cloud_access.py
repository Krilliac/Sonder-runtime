"""Pure policy text for explaining the hosted-tier opt-in boundary."""

from __future__ import annotations

LEGACY_ERROR_PREFIX = "ERROR:"
_TRUE = frozenset({"1", "true", "yes", "on"})


def cloud_allowed(environment) -> bool:
    """Return whether an explicit hosted/cloud opt-in is present."""
    return str(environment.get("SONDER_ALLOW_CLOUD", "")).strip().lower() in _TRUE


def has_legacy_error_prefix(value: object) -> bool:
    """Return whether a compatibility-boundary value is a legacy error."""
    return str(value or "").startswith(LEGACY_ERROR_PREFIX)


def cloud_disabled_message() -> str:
    """Explain how to opt into hosted tiers and where prompts are sent."""
    return (
        f"{LEGACY_ERROR_PREFIX} hosted/cloud tiers are disabled. Set SONDER_ALLOW_CLOUD=1 "
        "to opt in; prompts sent to cloud tiers leave this machine."
    )
