"""Adapters that bind concrete child runners to the application port."""
from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import replace
from threading import Event, Lock
from time import monotonic

from ..application.context import OperationContext
from ..application.execution.effect_journal import (
    EffectIntent,
    EffectJournalError,
    EffectState,
    settled_receipts,
)
from ..application.execution.effect_journal import (
    bound as bound_effect_journal,
)
from ..application.execution.worker_bindings import (
    AuthenticatedWorkerBinding,
    journaled_effect,
)
from ..application.ports.continuation_records import DurableChildSession
from ..application.ports.subagents import (
    InvalidSubagentRequest,
    SubagentBudget,
    SubagentHandle,
    SubagentRequest,
    SubagentSnapshot,
    SubagentStatus,
)
from ..application.subagents.checkpoint_provenance import (
    CheckpointResumeDecision,
    ChildResumeRefused,
    ProvenanceJournal,
    resumed_from,
    validate_checkpoint_resume,
    validate_uncheckpointed_resume,
)
from ..application.subagents.durable_continuation import (
    ContinuableCheckpoint,
    DurableCancellation,
    DurableContinuationService,
)
from .execution.subagent_dispatch_verifier import (
    DISPATCH_CONTRACT,
    DISPATCH_RECONCILIATION,
    MAX_DISPATCH_ATTEMPTS,
    REFUSED_RECEIPT_PREFIX,
    DurableSubagentDispatchVerifier,
    canonical_dispatch_request,
    dispatch_idempotency_key,
    dispatch_operation_id,
    dispatch_receipt_key,
    dispatch_refused_receipt_key,
    dispatch_request_digest,
)

_LOG = logging.getLogger(__name__)
_RECOVERABLE = frozenset({SubagentStatus.FAILED, SubagentStatus.TIMED_OUT})

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

    Concurrent identical spawns join: while this provider's runner for the
    child is live, a repeat of the exact request returns the live handle and
    composes no new binding; after it finished, the settled dispatch is
    reused.  A different request for the same child id is refused.  A
    dispatch refused synchronously before any durable admission is recorded
    as a ``failed`` (no-effect) attempt, and a later corrected dispatch is
    journaled as the next bounded attempt.

    With a ``provenance_journal`` the provider also owns the resume path:
    ``resume`` (and an exact repeat spawn of a crashed or recoverable child)
    validates the child checkpoint against the effect journal before claiming
    it, and runs the runner with the settled receipts bound so completed inner
    effects are consumed rather than re-invoked.  A refused validation raises
    ``ChildResumeRefused`` and the child stays ``recovery_required``.
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
        provenance_journal: ProvenanceJournal | None = None,
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
        if provenance_journal is not None and (
            effect_binding_factory is None
            or not callable(getattr(provenance_journal, "position", None))
        ):
            raise TypeError("provenance_journal requires an effect binding and a journal position")
        self._effect_binding_factory = effect_binding_factory
        self._dispatch_verifier = dispatch_verifier
        self._provenance_journal = provenance_journal
        # Journaled dispatch is serialized per provider, and children whose
        # runner this provider launched are tracked until the runner exits.
        # A repeat spawn of such a child must not compose a fresh binding:
        # composition runs restart recovery, which would fence the live
        # runner's in-flight inner effects as uncertain.
        self._dispatch_lock = Lock()
        self._live_runners: set[str] = set()

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
        if self._effect_binding_factory is None:
            return self._local_service.spawn(
                request, context, self._bounded_runner(request, context, runner, None, None),
            )
        child_id = request.child_id
        with self._dispatch_lock:
            if child_id in self._live_runners:
                # Idempotent join: the service returns the live handle for
                # the exact repeat request, or refuses a different one; it
                # can never start a second runner.
                return self._local_service.spawn(request, context, _refuse_redispatch)
            existing = self._local_service.record(child_id)
            if existing is not None and self._resumable(existing, request):
                return self._resume_locked(existing, context, runner)
            binding = self._effect_binding_factory(request, context)
            if not isinstance(binding, AuthenticatedWorkerBinding):
                raise TypeError("effect_binding_factory returned an invalid binding")
            gate = _DispatchGate()
            bounded_runner = self._bounded_runner(request, context, runner, binding, gate)
            self._live_runners.add(child_id)
            try:
                handle, launched = self._journaled_dispatch(
                    binding, request, context, bounded_runner,
                )
            except BaseException:
                self._live_runners.discard(child_id)
                gate.abort()
                raise
            if not launched:
                self._live_runners.discard(child_id)
            gate.publish()
            return handle

    def resume(self, child_id: str, context: OperationContext) -> SubagentHandle:
        """Resume a crashed or recoverable child from its validated checkpoint.

        Order: prove the old owner is gone (or the child is already
        recoverable), compose the journal binding (which claims a newer owner
        epoch and runs bounded verifier reconciliation), confirm the settled
        dispatch receipt, validate the checkpoint provenance against the
        journal, then claim the exact validated revision.  Any refusal leaves
        the child ``recovery_required`` and starts nothing.
        """
        if self._effect_binding_factory is None or self._provenance_journal is None:
            raise InvalidSubagentRequest("child resume requires an effect journal and provenance")
        with self._dispatch_lock:
            if child_id in self._live_runners:
                raise InvalidSubagentRequest("child runner is live in this provider")
            record = self._local_service.record(child_id)
            if record is None:
                raise InvalidSubagentRequest(f"unknown child_id {child_id!r}")
            request = record.request
            bounded = self._local_service.bounded_context(context, request.budget)
            runner = (self._runner_factory(request, bounded)
                      if self._runner_factory else self._runner)
            return self._resume_locked(record, context, runner)

    def _resumable(self, record: DurableChildSession, request: SubagentRequest) -> bool:
        """An exact repeat of a crashed or recoverable child routes to resume."""
        if self._provenance_journal is None or record.cancellation_requested:
            return False
        if dispatch_request_digest(record.request) != dispatch_request_digest(request):
            return False
        if record.status in _RECOVERABLE:
            return record.recovery_required
        return (
            record.status is SubagentStatus.RUNNING
            and not self._local_service.runner_alive(record.request.child_id)
        )

    def _resume_locked(
        self, record: DurableChildSession, context: OperationContext, runner: Runner,
    ) -> SubagentHandle:
        child_id = record.request.child_id
        if record.status is SubagentStatus.RUNNING:
            # Raises ContinuationCleanupRequired unless the recorded owner is
            # another, provably dead process.
            record = self._local_service.release_dead_owner(child_id)
        if record.status not in _RECOVERABLE or not record.recovery_required:
            raise InvalidSubagentRequest("child session is not recoverable")
        request = record.request
        context = self._local_service.bounded_context(context, request.budget)
        binding = self._effect_binding_factory(request, context)  # type: ignore[misc]
        if not isinstance(binding, AuthenticatedWorkerBinding):
            raise TypeError("effect_binding_factory returned an invalid binding")
        attempts = self._dispatch_attempts(binding, child_id)
        admitted = attempts[-1] if attempts else None
        if admitted is None or admitted.state is not EffectState.COMPLETED:
            raise InvalidSubagentRequest("child has no settled dispatch receipt to resume from")
        identity = self._dispatch_identity(request, len(attempts))
        admission = self._dispatch_verifier.admission(  # type: ignore[union-attr]
            child_id, attempt=len(attempts), **identity,
        )
        if (admission is None or admission.receipt_key != admitted.receipt_key
                or admitted.request_digest != identity["request_digest"]):
            raise EffectJournalError("settled subagent dispatch is not provable from the child store")
        if record.checkpoint is None:
            decision = validate_uncheckpointed_resume(
                self._provenance_journal, run_id=binding.run_id,
                worker_id=binding.worker_id, resumer_owner_epoch=binding.owner_epoch,
                admission_operations=frozenset(
                    dispatch_operation_id(child_id, attempt)
                    for attempt in range(1, len(attempts) + 1)
                ),
            )
        else:
            decision = validate_checkpoint_resume(
                record.checkpoint, self._provenance_journal, run_id=binding.run_id,
                worker_id=binding.worker_id, resumer_owner_epoch=binding.owner_epoch,
            )
        if not decision.allowed:
            _LOG.warning(
                "child resume refused: child=%s reason=%s",
                child_id, decision.reason.value if decision.reason else "",
            )
            raise ChildResumeRefused(decision)
        bounded_runner = self._bounded_runner(
            request, context, runner, binding, None, resume=decision,
        )
        self._live_runners.add(child_id)
        try:
            handle = self._local_service.resume(
                child_id, context, bounded_runner, expected_revision=record.revision,
            )
        except BaseException:
            self._live_runners.discard(child_id)
            raise
        _LOG.info(
            "child resumed from checkpoint: child=%s sequence=%s settled_receipts=%d",
            child_id, decision.sequence,
            len(decision.receipts) + len(decision.later_receipts),
        )
        return handle

    def _bounded_runner(
        self, request: SubagentRequest, context: OperationContext, runner: Runner,
        binding: AuthenticatedWorkerBinding | None, gate: _DispatchGate | None,
        *, resume: CheckpointResumeDecision | None = None,
    ) -> Runner:
        budget = request.budget
        child_id = request.child_id

        def bounded_runner(state, save, control):
            if binding is None:
                return run(state, save, control)
            try:
                if gate is not None:
                    gate.wait(self._receipt_wait(context))
                return run(state, save, control)
            finally:
                with self._dispatch_lock:
                    self._live_runners.discard(child_id)

        def run(state, save, control):
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
                with ExitStack() as scope:
                    # Inner tool effects join the same run, above the settled
                    # dispatch receipt.
                    scope.enter_context(bound_effect_journal(binding.binding()))
                    if resume is not None:
                        # A resumed runner consumes receipts settled before
                        # the crash; re-admitting one of them is refused.
                        scope.enter_context(resumed_from(resume))
                        scope.enter_context(settled_receipts({
                            key: receipt.receipt_key
                            for key, receipt in {
                                **resume.receipts, **resume.later_receipts,
                            }.items()
                        }))
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

        return bounded_runner

    @staticmethod
    def _dispatch_identity(request: SubagentRequest, attempt: int) -> dict[str, str]:
        return {
            "parent_id": request.parent_id,
            "idempotency_key": dispatch_idempotency_key(request, attempt),
            "request_digest": dispatch_request_digest(request),
        }

    @staticmethod
    def _dispatch_attempts(
        binding: AuthenticatedWorkerBinding, child_id: str,
    ) -> tuple[EffectIntent, ...]:
        """Read this child's dispatch attempts in order (bounded)."""
        get_intent = getattr(binding.journal, "get", None)
        if not callable(get_intent):
            return ()
        attempts: list[EffectIntent] = []
        for attempt in range(1, MAX_DISPATCH_ATTEMPTS + 1):
            try:
                intent = get_intent(f"{binding.run_id}:{dispatch_operation_id(child_id, attempt)}")
            except KeyError:
                intent = None
            if intent is None:
                break
            attempts.append(intent)
        return tuple(attempts)

    def _journaled_dispatch(
        self, binding: AuthenticatedWorkerBinding, request: SubagentRequest,
        context: OperationContext, bounded_runner: Runner,
    ) -> tuple[SubagentHandle, bool]:
        """Journal admission only; the caller keeps the runner gated.

        Returns the handle and whether this call admitted a new runner.
        """
        verifier = self._dispatch_verifier
        child_id = request.child_id
        if verifier is None or child_id is None:
            raise EffectJournalError("journaled dispatch requires a verifier and child id")
        attempts = self._dispatch_attempts(binding, child_id)
        attempt = len(attempts) + 1
        if attempts:
            prior = attempts[-1]
            prior_identity = self._dispatch_identity(request, len(attempts))
            if prior.state is EffectState.COMPLETED:
                if (prior.idempotency_key != prior_identity["idempotency_key"]
                        or prior.request_digest != prior_identity["request_digest"]):
                    # Same child identity, different request: refused before
                    # any journal write or admission.
                    raise InvalidSubagentRequest(
                        "child identity is already admitted for a different request"
                    )
                # Reusing a settled dispatch returns the admitted child only
                # while the durable child store still proves that exact
                # admission.
                admission = verifier.admission(child_id, attempt=len(attempts), **prior_identity)
                if admission is None or admission.receipt_key != prior.receipt_key:
                    raise EffectJournalError(
                        "settled subagent dispatch is not provable from the child store"
                    )
                return self._local_service.spawn(request, context, _refuse_redispatch), False
            if not (prior.state is EffectState.FAILED
                    and prior.receipt_key.startswith(REFUSED_RECEIPT_PREFIX)):
                # Unresolved or otherwise uncertain: never re-dispatch.
                raise EffectJournalError(
                    "subagent dispatch attempt is unresolved; reconciliation required"
                )
            if attempt > MAX_DISPATCH_ATTEMPTS:
                raise InvalidSubagentRequest("subagent dispatch attempt budget exhausted")
        operation_id = dispatch_operation_id(child_id, attempt)
        identity = self._dispatch_identity(request, attempt)
        dispatched: dict[str, object] = {}

        def dispatch() -> dict[str, object]:
            try:
                dispatched["handle"] = self._local_service.spawn(
                    request, context, bounded_runner,
                )
            except InvalidSubagentRequest as error:
                # A synchronous admission refusal is a failed dispatch only
                # when the durable store shows no admission of this request.
                if verifier.admission(child_id, attempt=attempt, **identity) is not None:
                    raise
                dispatched["refused"] = error
                return {
                    "contract": DISPATCH_CONTRACT,
                    "child_id": child_id,
                    "attempt": attempt,
                    "refused": type(error).__name__,
                }
            admission = verifier.admission(child_id, attempt=attempt, **identity)
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
                else dispatch_refused_receipt_key(child_id, attempt)
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
        return handle, True  # type: ignore[return-value]


__all__ = [
    "LocalSubagentProvider", "RunnerBoundSubagentProvider",
    "UnsupportedSubagentProvider",
]
