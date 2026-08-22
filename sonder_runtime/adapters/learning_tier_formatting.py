"""Pure presentation of the model tiers that feed learning."""

from collections.abc import Iterable, Mapping


def format_learning_tiers(
    tiers: Mapping[str, object],
    learning_tiers: Iterable[str],
    *,
    cloud_enabled: bool,
    cloud_tiers: Iterable[str] = (),
) -> str:
    """Render configured learning tiers without reading runtime state.

    The caller supplies the already-resolved tier mapping and policy.  Keeping
    those inputs outside this adapter makes the presentation deterministic and
    prevents the formatter from acquiring model, persistence, or environment
    dependencies.
    """
    enabled = set(learning_tiers)
    cloud_names = set(cloud_tiers)
    lines = ["learning tiers"]
    for tier, model in tiers.items():
        state = "on" if tier in enabled else "off"
        locality = "cloud" if _is_cloud_tier(tier, model, cloud_names) else "local"
        if locality == "cloud" and not cloud_enabled:
            state = "disabled"
        lines.append("  %s: %s (%s, %s)" % (tier, state, locality, model))
    if cloud_enabled:
        lines.append(
            "cloud tiers are available; opt into cloud learning explicitly with "
            "SONDER_LEARN_TIERS"
        )
    else:
        lines.append(
            "cloud tiers require SONDER_ALLOW_CLOUD=1; override learning with "
            "SONDER_LEARN_TIERS"
        )
    return "\n".join(lines)


def _is_cloud_tier(
    tier: object, model: object, cloud_tiers: set[str]
) -> bool:
    """Match the server's tier presentation rule without importing it."""
    if tier in cloud_tiers:
        return True
    name = str(model or "").lower()
    return "-cloud" in name or name.endswith(":cloud")
