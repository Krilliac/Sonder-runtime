"""HTTP helpers for Spanda R_sc on /v1/chat/completions.

Additive: default OFF. Request header ``X-Sonder-Spanda: 1`` enables one
request without changing global config. Never bypasses api-key auth.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping

from sonder_runtime.platform.spanda_config import SpandaConfig
from sonder_runtime.platform.spanda_rsc import compute_rsc

logger = logging.getLogger(__name__)

HEADER_ENABLE = "X-Sonder-Spanda"
HEADER_RSC = "X-Sonder-Spanda-Rsc"
HEADER_CLUSTERS = "X-Sonder-Spanda-Clusters"
HEADER_DECISION = "X-Sonder-Spanda-Decision"
HEADER_UNCERTAIN = "X-Sonder-Spanda-Uncertain"

_TRUTHY = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True)
class SpandaRequestPolicy:
    active: bool
    k: int
    threshold: float
    block: bool
    alpha: float
    sample_temperature: float


def _header_enabled(headers: Mapping[str, str] | None) -> bool:
    if not headers:
        return False
    raw = ""
    for key, value in headers.items():
        if key.lower() == HEADER_ENABLE.lower():
            raw = str(value).strip().lower()
            break
    return raw in _TRUTHY


def resolve_spanda_policy(
    config: SpandaConfig | None,
    headers: Mapping[str, str] | None,
) -> SpandaRequestPolicy:
    """Resolve effective Spanda policy for one request (default inactive)."""
    cfg = config or SpandaConfig()
    header_on = _header_enabled(headers)
    active = bool(cfg.enabled) or header_on
    return SpandaRequestPolicy(
        active=active,
        k=int(cfg.k),
        threshold=float(cfg.threshold),
        block=bool(cfg.block),
        alpha=float(cfg.alpha),
        sample_temperature=float(cfg.sample_temperature),
    )


def evaluate_samples(
    samples: list[str],
    *,
    alpha: float,
    threshold: float,
) -> dict[str, Any]:
    result = compute_rsc(samples, alpha=alpha)
    uncertain = result["rsc"] >= threshold
    decision = "uncertain" if uncertain else "consensus"
    result["uncertain"] = uncertain
    result["decision"] = decision
    logger.debug(
        "spanda evaluate: rsc=%s clusters=%s decision=%s",
        result["rsc"],
        result["n_clusters"],
        decision,
    )
    return result


def response_headers(evaluation: Mapping[str, Any]) -> dict[str, str]:
    return {
        HEADER_RSC: str(evaluation["rsc"]),
        HEADER_CLUSTERS: str(evaluation["n_clusters"]),
        HEADER_DECISION: str(evaluation["decision"]),
        HEADER_UNCERTAIN: "1" if evaluation["uncertain"] else "0",
    }


def uncertainty_error_body(
    evaluation: Mapping[str, Any],
    *,
    threshold: float,
    correlation_id: str = "",
) -> dict[str, Any]:
    """Structured body matching existing OpenAI-ish error style."""
    message = (
        "Spanda R_sc epistemic uncertainty exceeded threshold: "
        f"rsc={evaluation['rsc']} threshold={threshold} "
        f"clusters={evaluation['n_clusters']}. "
        "Dominant consensus is withheld because block=true. "
        "Caveat: Confident Mode Collapse — unanimous wrong answers can still "
        "score low R_sc."
    )
    body: dict[str, Any] = {
        "error": {
            "message": message,
            "type": "epistemic_uncertainty",
            "code": "SPANDA_RSC_UNCERTAIN",
            "rsc": evaluation["rsc"],
            "threshold": threshold,
            "n_clusters": evaluation["n_clusters"],
            "decision": evaluation["decision"],
            "dominant_answer": evaluation.get("dominant_answer", ""),
        }
    }
    if correlation_id:
        body["error"]["correlation_id"] = correlation_id
    return body


# Prefer 409 Conflict when blocking on uncertainty (matches existing conflict
# style); 422 is acceptable for semantic validation — we use 409.
BLOCK_STATUS = 409

EXPOSE_HEADERS = (
    f"{HEADER_RSC}, {HEADER_CLUSTERS}, {HEADER_DECISION}, {HEADER_UNCERTAIN}"
)


__all__ = [
    "HEADER_ENABLE",
    "HEADER_RSC",
    "HEADER_CLUSTERS",
    "HEADER_DECISION",
    "HEADER_UNCERTAIN",
    "BLOCK_STATUS",
    "EXPOSE_HEADERS",
    "SpandaRequestPolicy",
    "resolve_spanda_policy",
    "evaluate_samples",
    "response_headers",
    "uncertainty_error_body",
]
