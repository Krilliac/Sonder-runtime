"""Pure presentation helpers for runtime model readiness."""
from __future__ import annotations

BASE_LOCAL_TIERS = ("fast", "code", "general")
OPTIONAL_LOCAL_TIERS = ("reasoning", "vision")


def format_model_readiness(data: dict, *, base_local_tiers=BASE_LOCAL_TIERS,
                           optional_local_tiers=OPTIONAL_LOCAL_TIERS) -> list[str]:
    """Render the bounded operator-facing readiness summary for ``/runtime``."""
    if data.get("inventory_error"):
        return ["  readiness: unknown (local model inventory unavailable)"]
    local_models = data.get("local_models") or {}
    missing = {
        str(model or "").strip().casefold()
        for model in data.get("missing_models") or ()
        if str(model or "").strip()
    }

    def unavailable(model) -> bool:
        return str(model or "").strip().casefold() in missing

    capability_errors = data.get("capability_errors") or {}
    base_missing = [
        "%s=%s%s" % (
            tier,
            local_models.get(tier) or "(unset)",
            " (%s)" % capability_errors[tier] if tier in capability_errors else "",
        )
        for tier in base_local_tiers
        if not str(local_models.get(tier) or "").strip()
        or unavailable(local_models.get(tier))
        or tier in capability_errors
    ]
    lines = ["  readiness:"]
    if base_missing:
        lines.append("    local chat/code: requires %s" % ", ".join(base_missing))
    else:
        lines.append("    local chat/code: ready")
    embedding = str(data.get("embedding_model") or "").strip()
    if not embedding or unavailable(embedding) or "embedding" in capability_errors:
        lines.append(
            "    semantic memory: requires embedding model%s%s"
            % (
                " %s" % embedding if embedding else "",
                " (%s)" % capability_errors["embedding"]
                if "embedding" in capability_errors else "",
            )
        )
    else:
        lines.append("    semantic memory: ready (%s)" % embedding)
    for tier in optional_local_tiers:
        model = str(local_models.get(tier) or "").strip()
        if not model:
            lines.append("    %s: not configured (optional)" % tier)
        elif unavailable(model):
            lines.append("    %s: requires %s" % (tier, model))
        else:
            lines.append("    %s: configured (%s)" % (tier, model))
    return lines
