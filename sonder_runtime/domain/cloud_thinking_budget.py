"""Pure output-budget policy for hosted-model thinking requests."""

from __future__ import annotations


def ensure_prediction_budget(payload, minimum: int = 4096) -> None:
    """Ensure a request leaves room for thinking and its final answer.

    The policy only adjusts a positive, undersized integer ``num_predict``
    inside an options mapping.  Missing options, non-integer values, and
    unlimited or already-generous budgets are intentionally left unchanged.
    A shallow copy of the options mapping prevents callers from observing a
    partial update while the request payload is being assembled.
    """
    options = payload.get("options")
    if not isinstance(options, dict):
        return
    requested = options.get("num_predict")
    if isinstance(requested, int) and 0 < requested < minimum:
        payload["options"] = dict(options, num_predict=minimum)
