"""Pure policy for repairing a legacy live cloud-tier binding."""
from __future__ import annotations

from collections.abc import Mapping, MutableMapping


LEGACY_CLOUD_GENERAL_MODEL = "gpt-oss:120b-cloud"


def refresh_live_cloud_tiers(
    tiers: MutableMapping[str, object],
    environment: Mapping[str, object],
    *,
    default_cloud_general_model: object,
) -> None:
    """Repair the retired live ``cloud-general`` binding when appropriate.

    Live reload preserves the process' mutable tier map.  Only the exact
    historical binding is repaired, and an explicit operator preservation
    flag keeps that binding untouched.  The caller supplies both mappings and
    the current typed default so this policy has no process-global dependency.
    """
    preserve_legacy = str(
        environment.get("SONDER_PRESERVE_LEGACY_CLOUD_GENERAL", "") or ""
    ).strip().lower() in ("1", "true", "yes", "on")
    if (
        not preserve_legacy
        and tiers.get("cloud-general") == LEGACY_CLOUD_GENERAL_MODEL
    ):
        tiers["cloud-general"] = default_cloud_general_model


__all__ = ["LEGACY_CLOUD_GENERAL_MODEL", "refresh_live_cloud_tiers"]
