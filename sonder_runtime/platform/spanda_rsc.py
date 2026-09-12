"""Spanda-style Exact-Match Normalized Entropy (R_sc).

Vendored pure-Python implementation of the Spanda epistemic uncertainty
metric for offline / no-Rust-gateway deployments. Matches the production
lexical formula:

    normalize  -> casefold, strip punctuation and whitespace
    w_i        = |C_i| / K
    H_norm     = 0 if n==1 else (-Σ w_i ln w_i) / ln K
    R_sc       = alpha * H_norm + (1 - alpha) * (1 - w_max)

Caveat — Confident Mode Collapse: unanimous wrong answers still yield a
low R_sc. Lexical consensus is not ground-truth correctness.
"""
from __future__ import annotations

import collections
import math
import re
from typing import Any

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def normalize_answer(text: str) -> str:
    """Deterministic exact-match key: casefold, strip punct/space."""
    if not isinstance(text, str):
        text = str(text)
    cleaned = _PUNCT_RE.sub("", text.casefold())
    return _WS_RE.sub(" ", cleaned).strip()


def compute_rsc(
    answers: list[str],
    *,
    alpha: float = 0.5,
) -> dict[str, Any]:
    """Compute Spanda R_sc over K sampled assistant strings.

    Returns keys: rsc, h_norm, w_max, n_clusters, dominant_answer, K.
    """
    if not answers:
        raise ValueError("answers must be a non-empty list")
    if not isinstance(alpha, (int, float)) or isinstance(alpha, bool):
        raise TypeError("alpha must be a float")
    alpha_f = float(alpha)
    if not math.isfinite(alpha_f) or not 0.0 <= alpha_f <= 1.0:
        raise ValueError("alpha must be within [0.0, 1.0]")

    K = len(answers)
    normalized = [normalize_answer(a) for a in answers]
    counts = collections.Counter(normalized)
    weights = [count / K for count in counts.values()]
    n_clusters = len(counts)

    if n_clusters == 1 or K == 1:
        h_norm = 0.0
    else:
        h_norm = sum(-w * math.log(w) for w in weights) / math.log(K)

    w_max = max(weights)
    rsc = alpha_f * h_norm + (1.0 - alpha_f) * (1.0 - w_max)

    dominant_norm = counts.most_common(1)[0][0]
    dominant_answer = next(
        ans for ans, norm in zip(answers, normalized) if norm == dominant_norm
    )
    return {
        "rsc": round(rsc, 4),
        "h_norm": round(h_norm, 4),
        "w_max": round(w_max, 4),
        "n_clusters": n_clusters,
        "dominant_answer": dominant_answer,
        "K": K,
    }


__all__ = ["normalize_answer", "compute_rsc"]
