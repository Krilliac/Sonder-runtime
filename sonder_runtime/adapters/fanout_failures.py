"""Transport-failure classification and safe rendering for fanout receipts.

A fanout receipt records why a target failed as a closed, content-free enum
and a short rendering that never persists a provider body or an echoed
prompt; a zero-target plan is explained by skip reason counts alone. These
read the transport's ``ModelCallError``, so they live with the adapters.
Moved from ``server.py`` in the WP1 Three-Hundred-Eighteenth Slice with its
behaviour byte-for-byte intact.
"""
from __future__ import annotations

import math
import time

from sonder_runtime.adapters.model_transport import ModelCallError
from sonder_runtime.domain.fanout_redaction import redact_prompt_echo


def failure_class(exc):
    """Map a transport failure into the durable, non-content receipt enum."""
    if not isinstance(exc, ModelCallError):
        return "unknown"
    kind = str(exc.kind or "").casefold()
    direct = {
        "configuration": "configuration",
        "request": "request_rejected",
        "timeout": "timeout",
        "transport": "transport",
        "protocol": "protocol",
        "empty_response": "empty_response",
        "budget": "budget_exhausted",
        "cancelled": "cancelled",
    }
    if kind in direct:
        return direct[kind]
    if kind == "http":
        if exc.status == 429:
            return "throttled"
        if exc.status == 408:
            return "timeout"
        if exc.status in (402, 404, 410) or (exc.status is not None and exc.status >= 500):
            return "unavailable"
        if exc.status is not None and 400 <= exc.status < 500:
            return "request_rejected"
    return "unknown"


def safe_error(exc, prompt):
    """Render a useful failure without allowing an echoed prompt into a receipt."""
    if isinstance(exc, ModelCallError):
        # Provider-controlled details may contain only a *partial* request
        # excerpt, which cannot be safely removed with exact replacement.
        # Keep stable diagnostic class/status metadata, but never persist that
        # untrusted body in a durable receipt or event.
        rendered = "ERROR: fanout model failure (%s%s)" % (
            exc.kind,
            " HTTP %s" % exc.status if exc.status is not None else "",
        )
    else:
        rendered = "ERROR: model request failed (%s)" % type(exc).__name__
    # This also protects local exception messages that happen to echo the full
    # request.  Provider excerpts were excluded above rather than redacted.
    return redact_prompt_echo(rendered, prompt)[:4000]


def no_eligible_models_error(plan, scope):
    """Explain a zero-target plan without exposing model names or prompts."""
    counts = {}
    earliest_retry = None
    now = time.time()
    for row in plan.get("skipped", []):
        reason = str(row.get("reason") or "not eligible")[:160]
        counts[reason] = counts.get(reason, 0) + 1
        if reason == "health cooldown active":
            try:
                remaining = float(row.get("retry_after_ts")) - now
            except (TypeError, ValueError):
                remaining = 0
            if remaining > 0:
                earliest_retry = remaining if earliest_retry is None else min(earliest_retry, remaining)
    label = str(plan.get("scope") or scope or "local")
    if not counts:
        return ModelCallError(
            "configuration", "no eligible %s models are currently discovered." % label,
        )
    summary = "; ".join(
        "%s (%d)" % (reason, count)
        for reason, count in sorted(counts.items())
    )
    if earliest_retry is not None:
        retry_seconds = max(1, int(math.ceil(earliest_retry)))
        summary += "; earliest cooldown retry in about %ds" % retry_seconds
    return ModelCallError(
        "configuration",
        "no eligible %s models are currently available; skipped: %s." % (label, summary),
    )
