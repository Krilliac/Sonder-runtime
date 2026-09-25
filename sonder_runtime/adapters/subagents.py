"""Adapters that bind concrete child runners to the application port."""
from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import replace
from threading import Event
from time import monotonic

from ..application.context import OperationContext
from ..application.execution.effect_journal import (
    EffectJournalError,
    EffectState,
)
from ..application.execution.effect_journal import (
    bound as bound_effect_journal,
)
from ..application.execution.worker_bindings import (
    AuthenticatedWorkerBinding,
    journaled_effect,
)
from ..application.ports.subagents import (
    InvalidSubagentRequest,
    SubagentBudget,
    SubagentHandle,
    SubagentRequest,
    SubagentSnapshot,
)
from ..application.subagents.durable_continuation import (
    ContinuableCheckpoint,
    DurableCancellation,
    DurableContinuationService,
)
from .execution.subagent_dispatch_verifier import (
    DISPATCH_CONTRACT,
    DISPATCH_RECONCILIATION,
    DurableSubagentDispatchVerifier,
    canonical_dispatch_request,
    dispatch_idempotency_key,
    dispatch_operation_id,
    dispatch_receipt_key,
    dispatch_request_digest,
)

Runner = Callable[[Mapping[str, object], Callable[[Mapping[str, object], str | None], ContinuableCheckpoint], DurableCancellation], str]

# Receipt publication follows admission in the spawning thread.  A runner
# that still has no receipt after this bound fails closed instead of running
# above an unresolved dispatch intent.
_DISPATCH_RECEIPT_WAIT_SECONDS = 30.0


class RunnerBoundSubagentProvider:
    """Expose a durable continuation service through ``SubagentProvider``."""

    def __init__(self, service: DurableContinuationService, runner: Runner) -> None:
        self._service = service
        self._runner = runner

    def spawn(self, request: SubagentRequest, context: OperationContext) -> SubagentHandle:
        return self._service.spawn(request, context, self._runner)

    def snapshot(self, child_id: str) -> SubagentSnapshot:
        return self._service.snapshot(child_id)

    def cancel(self, child_id: str, *, reason: str = "cancellation requested") -> bool:
        return self._service.cancel(child_id, reason=reason)

    def cancel_unstarted(self, child_id: str, *, expected_revision: int,
                         reason: str = "cancellation requested") -> bool:
        return self._service.cancel_unstarted(
            child_id, expected_revision=expected_revision, reason=reason,
        )

    def close(self, timeout: float | None = None) -> bool:
        return self._service.close(timeout)


class UnsupportedSubagentProvider(InvalidSubagentRequest):
    """Raised when a caller asks the local adapter for an unconfigured backend."""


class _DispatchGate:
    """Hold a child runner until its dispatch receipt is durable.

    The continuation service starts the runner thread during admission.
    Without this gate the runner could begin inner effects while the
    ``subagent-dispatch`` intent is still unresolved; those effects would sit
    above an unresolved journal prefix and could not advance the settled
    high-water.
    """

    def __init__(self) -> None:
        self._event = Event()
        self._published = False

    def publish(self) -> None:
        self._published = True
        self._event.set()

    def abort(self) -> None:
        self._event.set()

    def wait(self, timeout: float) -> None:
        if not self._event.wait(timeout) or not self._published:
            raise RuntimeError("subagent dispatch receipt was not published")


def _refuse_redispatch(_state, _save, _control) -> str:
    # A settled dispatch may only return the durably admitted child.  Any
    # service path that would start a runner here would be a second start.
    raise RuntimeError("settled subagent dispatch cannot start a second runner")


class LocalSubagentProvider(RunnerBoundSubagentProvider):
    """Provider-neutral child port backed by the local durable runner.

    The adapter owns the local provider choice and requires callers to publish
    an explicit durable root before spawning.  Runner output and checkpoint
    writes are bounded by the request budget; unsupported provider names fail
    before any child is published.

    With an effect binding, admission is journaled as one bounded
    ``subagent-dispatch:{child_id}`` effect.  Its receipt names the durably
    admitted child, parent, idempotency key, canonical request digest and
    admitted revision, read back from the child store.  The runner is
    released only after that receipt commits and then runs under the same
    binding, so inner effects form a settled prefix above the dispatch.
    Runner completion is not part of the dispatch receipt.
    """

    def __init__(
        self,
        service: DurableContinuationService,
        runner: Runner | None = None,
        *,
        runner_factory: Callable[[SubagentRequest, OperationContext], Runner] | None = None,
        provider: str = "local",
        effect_binding_factory: Callable[[SubagentRequest, OperationContext], AuthenticatedWorkerBinding] | None = None,
        dispatch_verifier: DurableSubagentDispatchVerifier | None = None,
    ) -> None:
        if provider != "local":
            raise UnsupportedSubagentProvider(
                f"unsupported subagent provider: {provider!r}"
            )
        if runner is None and runner_factory is None:
            raise InvalidSubagentRequest("local provider requires a concrete runner")
        super().__init__(service, runner)
        self._runner_factory = runner_factory
        self._local_service = service
        if effect_binding_factory is not None and not callable(effect_binding_factory):
            raise TypeError("effect_binding_factory must be callable")
        if dispatch_verifier is not None and not isinstance(
            dispatch_verifier, DurableSubagentDispatchVerifier
        ):
            raise TypeError("dispatch_verifier must be a durable dispatch verifier")
        if effect_binding_factory is not None and dispatch_verifier is None:
            # The dispatch receipt is read back from the durable child store;
            # without that reader the journal could only trust memory.
            raise TypeError("effect_binding_factory requires a dispatch_verifier")
        self._effect_binding_factory = effect_binding_factory
        self._dispatch_verifier = dispatch_verifier

    def register_root(self, root_id: str, budget: SubagentBudget, *, owner_id: str = "") -> None:
        self._local_service.register_root(root_id, budget, owner_id=owner_id)

    @staticmethod
    def _receipt_wait(context: OperationContext) -> float:
        timeout = _DISPATCH_RECEIPT_WAIT_SECONDS
        if context.deadline_monotonic is not None:
            timeout = min(timeout, max(0.0, context.deadline_monotonic - monotonic()))
        return timeout

    def spawn(self, request: SubagentRequest, context: OperationContext) -> SubagentHandle:
        """Apply request ceilings around the concrete local runner."""
        self._local_service.require_parent(request.parent_id)
        if request.child_id is None:
            request = replace(request, child_id="child-" + uuid.uuid4().hex)
        budget = request.budget
        context = self._local_service.bounded_context(context, budget)
        runner = self._runner_factory(request, context) if self._runner_factory else self._runner
        binding = (
            self._effect_binding_factory(request, context)
            if self._effect_binding_factory is not None else None
        )
        if binding is not None and not isinstance(binding, AuthenticatedWorkerBinding):
            raise TypeError("effect_binding_factory returned an invalid binding")
        gate = _DispatchGate()
        if binding is None:
            gate.publish()

        def bounded_runner(state, save, control):
            gate.wait(self._receipt_wait(context))
            steps = 0

            def bounded_save(next_state, cursor=None):
                nonlocal steps
                steps += 1
                if budget.max_steps is not None and steps > budget.max_steps:
                    raise TimeoutError("subagent step budget exhausted")
                return save(next_state, cursor)

            if binding is None:
                output = runner(state, bounded_save, control)
            else:
                # Inner tool effects join the same run, above the settled
                # dispatch receipt.
                with bound_effect_journal(binding.binding()):
                    output = runner(state, bounded_save, control)
            if not isinstance(output, str):
                raise InvalidSubagentRequest("local runner output must be text")
            # Four UTF-8 characters is a conservative local token estimate;
            # the evidence envelope applies its own stricter bound downstream.
            if (
                budget.max_output_tokens is not None
                and len(output.encode("utf-8")) > budget.max_output_tokens * 4
            ):
                raise TimeoutError("subagent output budget exhausted")
            return output

        if binding is None:
            return self._local_service.spawn(request, context, bounded_runner)
        try:
            handle = self._journaled_dispatch(binding, request, context, bounded_runner)
        except BaseException:
            gate.abort()
            raise
        gate.publish()
        return handle

    def _journaled_dispatch(
        self, binding: AuthenticatedWorkerBinding, request: SubagentRequest,
        context: OperationContext, bounded_runner: Runner,
    ) -> SubagentHandle:
        """Journal admission only; the caller keeps the runner gated."""
        verifier = self._dispatch_verifier
        child_id = request.child_id
        if verifier is None or child_id is None:
            raise EffectJournalError("journaled dispatch requires a verifier and child id")
        operation_id = dispatch_operation_id(child_id)
        identity = {
            "parent_id": request.parent_id,
            "idempotency_key": dispatch_idempotency_key(request),
            "request_digest": dispatch_request_digest(request),
        }
        get_intent = getattr(binding.journal, "get", None)
        prior = None
        if callable(get_intent):
            try:
                prior = get_intent(f"{binding.run_id}:{operation_id}")
            except KeyError:
                prior = None
        if (
            prior is not None
            and prior.state is EffectState.COMPLETED
            and prior.idempotency_key == identity["idempotency_key"]
            and prior.request_digest == identity["request_digest"]
        ):
            # Reusing a settled dispatch returns the admitted child only while
            # the durable child store still proves that exact admission.
            admission = verifier.admission(child_id, **identity)
            if admission is None or admission.receipt_key != prior.receipt_key:
                raise EffectJournalError(
                    "settled subagent dispatch is not provable from the child store"
                )
            return self._local_service.spawn(request, context, _refuse_redispatch)

        dispatched: dict[str, object] = {}

        def dispatch() -> dict[str, object]:
            try:
                dispatched["handle"] = self._local_service.spawn(
                    request, context, bounded_runner,
                )
            except InvalidSubagentRequest as error:
                # A synchronous admission refusal is a failed dispatch only
                # when the durable store shows no admission of this request.
                if verifier.admission(child_id, **identity) is not None:
                    raise
                dispatched["refused"] = error
                return {
                    "contract": DISPATCH_CONTRACT,
                    "child_id": child_id,
                    "refused": type(error).__name__,
                }
            admission = verifier.admission(child_id, **identity)
            if admission is None:
                raise EffectJournalError(
                    "subagent dispatch admission is not durably provable"
                )
            return admission.receipt()

        journaled_effect(
            binding,
            operation_id=operation_id,
            idempotency_key=identity["idempotency_key"],
            request=canonical_dispatch_request(request),
            invoke=dispatch,
            receipt_key=lambda receipt: (
                dispatch_receipt_key(child_id, receipt["admitted_revision"])
                if "admitted_revision" in receipt
                else f"subagent-dispatch-refused:{child_id}"
            ),
            reconciliation=DISPATCH_RECONCILIATION,
            success=lambda receipt: "admitted_revision" in receipt,
        )
        refused = dispatched.get("refused")
        if isinstance(refused, InvalidSubagentRequest):
            raise refused
        handle = dispatched.get("handle")
        if handle is None:
            raise EffectJournalError("subagent dispatch returned no child handle")
        return handle  # type: ignore[return-value]


__all__ = [
    "LocalSubagentProvider", "RunnerBoundSubagentProvider",
    "UnsupportedSubagentProvider",
]
