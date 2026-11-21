"""Pure policy for the live cloud-tier bindings (legacy repair and default resync)."""
from __future__ import annotations

from collections.abc import Collection, Mapping, MutableMapping, MutableSet


def refresh_live_cloud_tiers(
    tiers: MutableMapping[str, object],
    environment: Mapping[str, object],
    *,
    default_cloud_general_model: object,
    legacy_cloud_general_model: str,
) -> None:
    """Repair the retired live ``cloud-general`` binding when appropriate.

    Live reload preserves the process' mutable tier map.  Only the exact
    historical binding is repaired, and an explicit operator preservation
    flag keeps that binding untouched.  The caller supplies both mappings and
    the current typed default so this policy has no process-global dependency.
    """
    preserve_legacy = _preserve_legacy(environment)
    if (
        not preserve_legacy
        and tiers.get("cloud-general") == legacy_cloud_general_model
    ):
        tiers["cloud-general"] = default_cloud_general_model


def _preserve_legacy(environment: Mapping[str, object]) -> bool:
    return str(
        environment.get("SONDER_PRESERVE_LEGACY_CLOUD_GENERAL", "") or ""
    ).strip().lower() in ("1", "true", "yes", "on")


def resync_cloud_tiers(
    tiers: MutableMapping[str, object],
    environment: Mapping[str, object],
    *,
    applied: MutableMapping[str, MutableSet[str]],
    current_defaults: Mapping[str, str],
    retired: Collection[str],
    legacy_cloud_general_model: str,
) -> list[tuple[str, object, str]]:
    """Move default-derived cloud tiers to the current model-list defaults.

    ``applied`` holds, per cloud tier (``code``/``general``), the casefolded
    defaults this process has bound (seeded from the startup snapshot); the
    current default is added on every call.  For ``tier = "cloud-" + T``:

    1. a legacy binding is left alone while the preserve flag is set;
    2. a retired binding moves to the current default (unless that is
       retired too); the environment variable itself is never changed;
    3. a tier bound by a live ``SONDER_CLOUD_<T>`` is left alone;
    4. a binding that is a previously applied default moves to the current one.

    Membership (not "the last applied value") decides step 4, so resyncing a
    copy of the tier map (as the prepared workbench does) cannot stop the
    process map from following later.  Returns ``(tier, before, after)`` per
    move.  The caller owns locking for ``applied``; ``tiers`` is mutated in
    place exactly as the legacy repair does.
    """
    retired_cf = {str(name).casefold() for name in retired}
    preserve = _preserve_legacy(environment)
    moves: list[tuple[str, object, str]] = []
    for short, default in current_defaults.items():
        tier = "cloud-" + short
        if tier not in tiers or not isinstance(default, str) or not default:
            continue
        seen = applied.setdefault(short, set())
        seen.add(default.casefold())
        binding = tiers[tier]
        if not isinstance(binding, str):
            continue
        folded = binding.strip().casefold()
        if (preserve and binding == legacy_cloud_general_model) or folded == default.casefold():
            continue
        if default.casefold() in retired_cf:
            continue
        env_value = str(environment.get("SONDER_CLOUD_" + short.upper(), "") or "").strip()
        if folded in retired_cf or (
            not (env_value and env_value.casefold() not in retired_cf) and folded in seen
        ):
            tiers[tier] = default
            moves.append((tier, binding, default))
    return moves


__all__ = ["refresh_live_cloud_tiers", "resync_cloud_tiers"]
