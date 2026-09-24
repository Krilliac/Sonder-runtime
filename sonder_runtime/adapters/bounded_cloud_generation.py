"""Hard-bounded aggregate output for a hosted agent run.

Every hosted generation is wrapped so the per-call ceiling and the run's
total output allowance are enforced from provider usage or a conservative
estimate, never from the model's own claims, and an exhausted allowance
refuses before the next request is sent. It raises and catches the
transport's ``ModelCallError``, so it lives with the adapters. Moved from
``server.py`` in the WP1 Three-Hundred-Twenty-Fourth Slice with its
behaviour byte-for-byte intact.
"""
from __future__ import annotations

import threading
import time

from sonder_runtime.adapters.model_transport import ModelCallError
from sonder_runtime.domain.context_formatting import rough_token_count
from sonder_runtime.domain.model_usage import usage_count
from sonder_runtime.domain.token_bucket import TokenBucket

# A hosted agent decision may contain a complete bounded file_write payload.
# The per-call ceiling accommodates substantial native arguments, but exact
# 64 KiB payloads may still require chunks because characters are not tokens.
CLOUD_AGENT_NUM_PREDICT = 16384
CLOUD_AGENT_OUTPUT_BUDGET = 65536
# The hosted agent takes at most 20 model steps plus a bounded claim review.
# Permit that ordinary run in a burst, while bounding repeated requests in a
# longer-lived shared run independently of provider 429 and output length.
CLOUD_AGENT_REQUEST_BURST = 24
CLOUD_AGENT_REQUESTS_PER_MINUTE = 12
CLOUD_AGENT_MAX_REQUESTS = 48


def bounded_cloud_generate(
    gen,
    *,
    per_call_limit=CLOUD_AGENT_NUM_PREDICT,
    total_budget=CLOUD_AGENT_OUTPUT_BUDGET,
    budget_state=None,
):
    """Hard-bound aggregate hosted output while preserving actual usage data."""
    per_call_limit = max(1, int(per_call_limit))
    total_budget = max(per_call_limit, int(total_budget))
    if budget_state is None:
        budget_state = {"spent": 0, "total": total_budget}
    else:
        budget_state.setdefault("spent", 0)
        budget_state.setdefault("total", total_budget)
        total_budget = max(1, int(budget_state["total"]))
    # The application passes the same host-owned state to the main agent and
    # any negative-claim reviewer. The bucket and lock are per-run, not global
    # process policy; transient model failures still occupy one admission.
    request_lock = budget_state.setdefault("_request_lock", threading.RLock())
    request_bucket = budget_state.setdefault(
        "_request_bucket",
        TokenBucket(CLOUD_AGENT_REQUEST_BURST, CLOUD_AGENT_REQUESTS_PER_MINUTE / 60),
    )
    budget_state.setdefault("request_count", 0)

    def bounded(prompt, history=None):
        with request_lock:
            spent = max(0, int(budget_state.get("spent", 0)))
            remaining = total_budget - spent
            if remaining <= 0:
                raise ModelCallError(
                    "budget",
                    "the bounded %d-token allowance for this agent run was consumed"
                    % total_budget,
                    attempts=0,
                    cloud=True,
                )
            if max(0, int(budget_state["request_count"])) >= CLOUD_AGENT_MAX_REQUESTS:
                raise ModelCallError(
                    "budget", "hosted agent request count reached its run ceiling",
                    attempts=0, cloud=True,
                )
            admission = request_bucket.try_acquire(now=time.monotonic())
            if not admission.allowed:
                raise ModelCallError(
                    "rate", "hosted agent request rate exceeded its burst ceiling",
                    attempts=0, cloud=True,
                    retry_after_seconds=admission.retry_after,
                )
            budget_state["request_count"] += 1
            call_limit = min(per_call_limit, remaining)
            try:
                gen.num_predict_override = call_limit
            except (AttributeError, TypeError):
                pass
            try:
                content = gen(prompt, history=history)
            except ModelCallError as error:
                usage = dict(getattr(gen, "last_usage", None) or {})
                # Failed attempted requests consume their full ceiling; a
                # call refused before transport still consumes rate admission.
                charged = call_limit if error.attempts > 0 else 0
                spent += charged
                budget_state["spent"] = spent
                bounded.last_usage = usage
                bounded.last_response_meta = dict(
                    getattr(gen, "last_response_meta", None) or {}
                )
                bounded.output_tokens_used = spent
                raise
            finally:
                try:
                    gen.num_predict_override = None
                except (AttributeError, TypeError):
                    pass
            usage = dict(getattr(gen, "last_usage", None) or {})
            reported = usage_count(usage.get("tokens_out"))
            estimated = max(1, rough_token_count(content))
            # Provider usage metadata is external input. Never let a zero or
            # implausibly low count make nonempty/native-tool output free.
            charged = max(reported or 0, estimated)
            spent += charged
            budget_state["spent"] = spent
            bounded.last_usage = usage
            bounded.last_response_meta = dict(
                getattr(gen, "last_response_meta", None) or {}
            )
            bounded.output_tokens_used = spent
            return content

    bounded.last_usage = {}
    bounded.last_response_meta = {}
    bounded.output_tokens_used = 0
    bounded.output_token_budget = total_budget
    bounded.output_budget_state = budget_state
    return bounded
