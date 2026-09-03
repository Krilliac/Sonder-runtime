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

from sonder_runtime.adapters.model_transport import ModelCallError
from sonder_runtime.domain.context_formatting import rough_token_count
from sonder_runtime.domain.model_usage import usage_count


# A hosted agent decision may contain a complete bounded file_write payload.
# The per-call ceiling accommodates substantial native arguments, but exact
# 64 KiB payloads may still require chunks because characters are not tokens.
CLOUD_AGENT_NUM_PREDICT = 16384
CLOUD_AGENT_OUTPUT_BUDGET = 65536


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

    def bounded(prompt, history=None):
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
        call_limit = min(per_call_limit, remaining)
        try:
            gen.num_predict_override = call_limit
        except (AttributeError, TypeError):
            pass
        try:
            content = gen(prompt, history=history)
        except ModelCallError as error:
            usage = dict(getattr(gen, "last_usage", None) or {})
            charged = usage_count(usage.get("tokens_out"))
            # Empty/failed hosted responses may not expose usage to the
            # caller. Charge the full request ceiling so repeated failures
            # cannot evade the aggregate budget.
            if error.attempts <= 0:
                charged = 0
            else:
                charged = call_limit
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
