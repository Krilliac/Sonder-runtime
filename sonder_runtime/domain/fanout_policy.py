"""Pure catalog-target eligibility policies for model fanout."""

from __future__ import annotations

import logging

from sonder_runtime.domain.model_capabilities import fanout_capabilities

logger = logging.getLogger(__name__)


_KNOWN_VISION_ONLY_MODEL_FAMILIES = frozenset(
    {
        "bakllava",
        "llama3.2-vision",
        "llava",
        "minicpm-v",
        "moondream",
    }
)
_GENERATIVE_CAPABILITIES = frozenset(
    {"completion", "chat", "generate", "text-generation"}
)


def _normalized_values(raw: object) -> set[str]:
    if isinstance(raw, str):
        values = (raw,)
    elif isinstance(raw, (list, tuple, set)):
        values = raw
    else:
        return set()
    return {str(value).strip().lower() for value in values if str(value).strip()}


def nonchat_reason(record: object) -> str:
    """Return why a catalog record must not be used as a chat target.

    The policy is deliberately explicit-input and side-effect free. Catalog
    metadata may be incomplete, so unknown records remain eligible for a
    bounded probe while positively identified embedding and vision targets
    are rejected before fanout spends a model slot.
    """
    record = record if isinstance(record, dict) else {}
    model_name = record.get("name") or record.get("model") or "?"
    details = record.get("details") if isinstance(record.get("details"), dict) else {}
    capabilities = fanout_capabilities(record)
    logger.debug(f"nonchat_reason: model={model_name!r}, capabilities={capabilities}")
    if capabilities & _GENERATIVE_CAPABILITIES:
        return ""
    if "embedding" in capabilities:
        logger.warning(f"model {model_name!r} excluded from fanout: embedding-only model cannot serve chat requests")
        logger.debug(f"nonchat_reason: rejecting {model_name!r} as embedding-only")
        return "embedding-only capability"
    if "vision" in capabilities:
        logger.warning(f"model {model_name!r} excluded from fanout: vision-only model cannot serve text chat requests")
        logger.debug(f"nonchat_reason: rejecting {model_name!r} as vision-only")
        return "vision-only capability"

    families = _normalized_values(details.get("family")) | _normalized_values(
        details.get("families")
    )
    if not families:
        name = str(record.get("name") or record.get("model") or "").strip().casefold()
        families = {name.rsplit("/", 1)[-1].split(":", 1)[0]}
    if families & _KNOWN_VISION_ONLY_MODEL_FAMILIES:
        logger.warning(f"model {model_name!r} excluded from fanout: belongs to known vision-only family {families & _KNOWN_VISION_ONLY_MODEL_FAMILIES}")
        logger.debug(f"nonchat_reason: rejecting {model_name!r}, family in vision-only list")
        return "known vision-only model family"
    return ""


def declares_generative_capability(record: object) -> bool:
    """Whether a catalog record explicitly declares a text-generation surface.

    Unknown records remain eligible for a bounded probe, but synthesis must
    require a positive local chat/completion declaration before spending a
    model slot on a multi-answer prompt.
    """
    result = bool(fanout_capabilities(record) & _GENERATIVE_CAPABILITIES)
    logger.debug(f"declares_generative_capability: {result} for record={repr(record)[:120]}")
    return result


__all__ = ["declares_generative_capability", "nonchat_reason"]
