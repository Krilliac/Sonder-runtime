"""Advisory model-health recording and cooldowns for fanout failures.

A target that just timed out, vanished or returned malformed output is
cooled down so the next all-model request does not repeat the failure;
caller and prompt failures stay eligible. Cloud cooldowns honour provider
retry hints and local failures back off exponentially with an hourly cap.
It persists through the fanout store and reads the transport's
``ModelCallError``, so it lives with the adapters. Moved from ``server.py``
in the WP1 Three-Hundred-Twenty-Fifth Slice with its behaviour byte-for-byte
intact.
"""
from __future__ import annotations

import time

from sonder_runtime.adapters.fanout_failures import safe_error
from sonder_runtime.adapters.model_transport import ModelCallError
from sonder_runtime.adapters.persistence import fanout_store


def record_health(model, exc, prompt, *, is_cloud_model_name):
    """Record advisory model health and cool down repeatable model failures.

    A fanout is explicitly opt-in, but repeating a target that just timed out,
    vanished, or returned malformed output makes the next "all models" request
    slower without adding an answer.  Keep caller/prompt failures eligible: a
    bad request is not evidence that the local model is unhealthy.  Cloud
    cooldowns preserve provider retry hints; local failures use a short fixed
    cooldown because there is no upstream throttle contract to honor.

    ``is_cloud_model_name(model)`` classifies the target; it is injected so
    the root delegate keeps the routing classifier's monkeypatch seam.
    """
    if exc is None:
        fanout_store.record_model_health(model, model_class="cloud" if is_cloud_model_name(model) else "local", success=True)
        return
    disabled_until = None
    availability_failure = False
    if isinstance(exc, ModelCallError):
        if is_cloud_model_name(model) and exc.status in (402, 404, 410):
            disabled_until = time.time() + 3600
        elif is_cloud_model_name(model) and exc.status == 429:
            disabled_until = time.time() + (exc.retry_after_seconds or 60)
        elif (
            is_cloud_model_name(model)
            and exc.retry_after_seconds is not None
            and (exc.transient or exc.kind in {"timeout", "transport", "protocol", "empty_response"})
        ):
            # Providers can throttle or shed load with transient statuses other
            # than 429 (for example 503).  An explicit Retry-After remains
            # authoritative for every transient cloud failure, not just 429.
            disabled_until = time.time() + exc.retry_after_seconds
        elif exc.status in (404, 410):
            # The tag disappeared from Ollama after the immutable run snapshot
            # was created. Avoid rediscovering and failing it on every fanout.
            disabled_until = time.time() + 3600
            availability_failure = True
        elif exc.transient or exc.kind in {"timeout", "transport", "protocol", "empty_response"}:
            # These identify the model/daemon response path, not the prompt.
            # Back off repeated availability failures instead of making every
            # frequent all-model request re-probe the same unhealthy local or
            # cloud target.  A cloud provider's explicit 429 Retry-After above
            # remains authoritative; this covers unavailable providers that
            # offer no retry contract.
            # Cap at an hour so recovery remains automatic without operator
            # intervention. A successful model call resets the stored count.
            previous = fanout_store.get_model_health(model)
            try:
                failure_count = max(0, int((previous or {}).get("availability_failure_count", 0))) + 1
            except (TypeError, ValueError):
                failure_count = 1
            delay_seconds = min(3600, 300 * (2 ** min(4, failure_count - 1)))
            disabled_until = time.time() + delay_seconds
            availability_failure = True
    fanout_store.record_model_health(
        model, model_class="cloud" if is_cloud_model_name(model) else "local",
        error=safe_error(exc, prompt), disabled_until=disabled_until,
        counts_toward_backoff=availability_failure,
    )
