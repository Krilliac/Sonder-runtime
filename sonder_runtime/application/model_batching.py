"""Bounded independent completions through the existing model gateway port.

Only a worker-sized window is admitted. A deadline or cancellation prevents
later admission; already admitted calls finish through their gateway's own
control boundary. Successful siblings remain available after another fails.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, wait
from contextvars import copy_context
from itertools import islice

from .context import OperationContext
from .ports.model_gateway import ModelBatchOutcome, ModelGateway, ModelRequest
from .ports.runtime_threads import ThreadPoolExecutor as owned_runtime_pool
from ..domain.common.errors import (
    Cancelled, DeadlineExceeded, InternalFailure, InvalidInput, SonderError,
)

MAX_BATCH_REQUESTS = 64
MAX_BATCH_WORKERS = 8


def _control_error(context: OperationContext) -> SonderError | None:
    if context.cancellation.cancelled:
        return Cancelled("model batch cancelled before request admission")
    if context.expired:
        return DeadlineExceeded("model batch deadline elapsed before request admission")
    return None


def generate_batch(
    gateway: ModelGateway, requests: Sequence[ModelRequest], context: OperationContext,
    *, max_workers: int = 2,
    validate_request: Callable[[ModelRequest], object] | None = None,
) -> tuple[ModelBatchOutcome, ...]:
    """Validate the entire batch, then preserve one outcome per original input.

    The provider may supply a non-dispatching request validator. Invalid batch
    shape/options raise before any call; provider/control failures are outcomes.
    Unexpected worker failures become content-free InternalFailure values, so
    their exception text cannot leak credentials or discard successful siblings.
    """
    if type(max_workers) is not int or not 1 <= max_workers <= MAX_BATCH_WORKERS:
        raise InvalidInput("model batch max_workers must be an integer from 1 to 8")
    if (not isinstance(requests, Sequence) or isinstance(requests, (str, bytes))
            or len(requests) > MAX_BATCH_REQUESTS):
        raise InvalidInput("model batch requires a finite sequence of at most 64 requests")
    # Bound even a custom Sequence whose iterator disagrees with its length.
    items = tuple(islice(iter(requests), MAX_BATCH_REQUESTS + 1))
    if len(items) > MAX_BATCH_REQUESTS:
        raise InvalidInput("model batch exceeds 64 requests")
    for request in items:
        if (not isinstance(request, ModelRequest) or not isinstance(request.prompt, str)
                or not request.prompt.strip() or request.stream):
            raise InvalidInput("model batch requires nonempty non-streaming ModelRequests")
        if validate_request is not None:
            validate_request(request)
    if not items:
        return ()
    stopped = _control_error(context)
    if stopped is not None:
        return tuple(ModelBatchOutcome(error=stopped) for _ in items)

    def invoke(request: ModelRequest) -> ModelBatchOutcome:
        try:
            stopped = _control_error(context)
            if stopped is not None:
                return ModelBatchOutcome(error=stopped)
            return ModelBatchOutcome(response=gateway.generate(request, context))
        except SonderError as error:
            return ModelBatchOutcome(error=error)
        except Exception:  # provider bugs must not erase already billed siblings
            return ModelBatchOutcome(error=InternalFailure("model batch request failed unexpectedly"))

    outcomes: list[ModelBatchOutcome | None] = [None] * len(items)
    pending = {}
    next_index = 0
    try:
        with owned_runtime_pool(max_workers=min(max_workers, len(items)),
                                thread_name_prefix="model-batch") as pool:
            while next_index < len(items) or pending:
                while next_index < len(items) and len(pending) < max_workers:
                    stopped = _control_error(context)
                    if stopped is not None:
                        for index in range(next_index, len(items)):
                            outcomes[index] = ModelBatchOutcome(error=stopped)
                        next_index = len(items)
                        break
                    try:
                        # One independent context per submission, even when the
                        # same executor thread later serves another batch item.
                        future = pool.submit(copy_context().run, invoke, items[next_index])
                    except Exception:
                        for index in range(next_index, len(items)):
                            outcomes[index] = ModelBatchOutcome(error=InternalFailure(
                                "model batch worker admission failed",
                            ))
                        next_index = len(items)
                        break
                    pending[future] = next_index
                    next_index += 1
                if pending:
                    completed, _ = wait(pending, timeout=0.05, return_when=FIRST_COMPLETED)
                    for future in completed:
                        index = pending.pop(future)
                        try:
                            outcomes[index] = future.result()
                        except Exception:
                            # An owned executor may fail cleanup outside invoke.
                            # That cannot authorize replay or erase other results.
                            outcomes[index] = ModelBatchOutcome(error=InternalFailure(
                                "model batch worker failed before publishing its result",
                            ))
    except Exception:
        # Factory construction, ownership admission and context entry/cleanup
        # can fail before submit. Never leak host details or replay siblings.
        for index, outcome in enumerate(outcomes):
            if outcome is None:
                outcomes[index] = ModelBatchOutcome(error=InternalFailure(
                    "model batch worker ownership unavailable",
                ))
    # Every input is either admitted once or explicitly refused above.
    return tuple(outcome if outcome is not None else ModelBatchOutcome(
        error=InternalFailure("model batch result unavailable"),
    ) for outcome in outcomes)
