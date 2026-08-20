"""Pure policy text for explaining the hosted-tier opt-in boundary."""

from __future__ import annotations

LEGACY_ERROR_PREFIX = "ERROR:"


def has_legacy_error_prefix(value: object) -> bool:
    """Return whether a compatibility-boundary value is a legacy error."""
    return str(value or "").startswith(LEGACY_ERROR_PREFIX)


def cloud_disabled_message() -> str:
    """Explain how to opt into hosted tiers and where prompts are sent."""
    return (
        f"{LEGACY_ERROR_PREFIX} hosted/cloud tiers are disabled. Set SONDER_ALLOW_CLOUD=1 "
        "to opt in; prompts sent to cloud tiers leave this machine."
    )
