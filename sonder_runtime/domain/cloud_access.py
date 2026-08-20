"""Pure policy text for explaining the hosted-tier opt-in boundary."""

from __future__ import annotations


def cloud_disabled_message() -> str:
    """Explain how to opt into hosted tiers and where prompts are sent."""
    return (
        "ERROR: hosted/cloud tiers are disabled. Set SONDER_ALLOW_CLOUD=1 "
        "to opt in; prompts sent to cloud tiers leave this machine."
    )
