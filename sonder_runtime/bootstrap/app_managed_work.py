"""Private, bounded composition of durable app work and managed host execution."""

from ..platform.runtime_threads import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
import hashlib
import threading
import time
import uuid

from ..application.ports.app_control import (
    CommandKey,
    CommandConflict,
    CapacityExceeded,
    NotFound,
    OutcomeUnknown,
)
from ..application.ports.app_managed_work import (
    PreparedAppWork,
    WorkInterruption,
    WorkCompletionEvidence,
    WorkVerificationPending,
    canonical_digest,
)
from ..application.ports.host_turn_links import FinalizedHostResult
from ..application.ports.lane_continuation import GrantedApprovalEvidence
from .managed_conversation import ManagedConversationLifetime


def dispatch_approval_arguments(work):
    if type(work) is not PreparedAppWork:
        raise TypeError("typed immutable work approval required")
    work.__post_init__()
    return dict(
        operation="app_managed_work",
        work_id=work.work_id,
        work_digest=work.digest,
        plan_digest=work.plan.digest,
        project_handle=work.binding.grant.project_handle,
        plan=asdict(work.plan),
    )


def dispatch_approval_digest(work):
    from ..adapters.security.permission_policy import PermissionPolicyProvider

    return PermissionPolicyProvider().call_digest(
        "workspace_run", dispatch_approval_arguments(work)
    )


@dataclass(eq=False, repr=False)
class _Run:
    selection: object
    lease: object
    record: object
    lifetime: object = None
    future: object = None
    finished: bool = False
    callback_exited: bool = False
    submission_failed: bool = False
    lease_released: bool = False
    selection_released: bool = False
    cleanup_lock: object = field(default_factory=threading.RLock)


class AppManagedWorkDispatcher:
    """Host-only service. Typed work/evidence values are not authority."""

    def __init__(
        self,
        authority,
        workbench,
        *,
        lifetime_factory,
        authorize_dispatch,
        terminal_eligibility,
        max_workers=1,
        max_retained=32,
        application=None,
    ):
        if any(
            not callable(value)
            for value in (lifetime_factory, authorize_dispatch, terminal_eligibility)
        ):
            raise TypeError("trusted managed work callbacks required")
        if (
            type(max_workers) is not int
            or not 1 <= max_workers <= 8
            or type(max_retained) is not int
            or not max_workers <= max_retained <= 128
        ):
            raise ValueError("bounded managed work capacity required")
        if authority is None or workbench is None:
            raise TypeError("private authority and prepared workbench required")
        self.authority, self.workbench = authority, workbench
        self._application = application
        self._factory, self._authorize, self._eligibility = (
            lifetime_factory,
            authorize_dispatch,
            terminal_eligibility,
        )
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="app-managed-work"
        )
        self._slots = threading.BoundedSemaphore(max_workers)
        self._capacity = max_retained
        self._lock = threading.RLock()
        self._runs = {}
        self._condition = threading.Condition(self._lock)
        self._submitting = 0
        self._closed = False
        self._incarnation = uuid.uuid4().hex

    @property
    def application(self):
        """Exact host-owned Application; None is legacy unowned compatibility."""
        return self._application

    def _scope(self, selection, work_id):
        return dict(
            principal_id=selection.control.principal_id,
            control_session_id=selection.control.control_session_id,
            work_id=work_id,
        )

    def _read(self, selection, work_id):
        def read(tx):
            record = tx.read_work(**self._scope(selection, work_id))
            if record is None:
                raise NotFound("work unavailable")
            if (
                record.prepared.binding != selection.binding
                or record.prepared.selection != selection.slot
                or record.prepared.account_session_ref != selection.account.reference
            ):
                raise PermissionError("work does not match exact live selection")
            return record

        return self.authority.work_atomic(selection, selection.context, read)

    def status(self, selection, *, work_id):
        return self._read(selection, work_id)

    def prepare(self, selection, *, command_id, request):
        with self._lock:
            if self._closed:
                raise PermissionError("managed dispatcher is closed")
        plan = self.workbench.prepare_workbench(request, selection.context)
        command = CommandKey(
            selection.control.principal_id,
            "control:" + selection.control.control_session_id,
            command_id,
        )
        work_id = hashlib.sha256(
            (
                command.principal_id
                + "\0"
                + command.session_scope
                + "\0"
                + command.command_id
            ).encode()
        ).hexdigest()
        context = selection.context
        context_digest = canonical_digest(
            dict(
                principal=context.principal_id,
                auth=context.auth_level,
                source=context.source,
                roots=[str(p) for p in context.workspace_roots],
                cloud=context.cloud_allowed,
                remote=context.remote_ollama_allowed,
                tools=selection.allowed_tools,
            )
        )

        def prepare(tx):
            prior = tx.read_work(**self._scope(selection, work_id))
            if prior is not None:
                work = prior.prepared
                if (
                    work.command != command
                    or work.plan != plan
                    or work.context_digest != context_digest
                    or work.binding != selection.binding
                    or work.selection != selection.slot
                    or work.account_session_ref != selection.account.reference
                ):
                    raise CommandConflict("immutable work preparation changed")
            else:
                now = time.time()
                expires = min(
                    selection.binding.expires_at,
                    selection.control.expires_at,
                    selection.account.expires_at,
                    now + max(0, context.deadline_monotonic - time.monotonic()),
                )
                work = PreparedAppWork(
                    work_id,
                    command,
                    selection.binding,
                    selection.slot,
                    selection.account.reference,
                    plan,
                    context_digest,
                    now,
                    expires,
                )
            return tx.prepare_work(work)

        return self.authority.work_atomic(selection, context, prepare)

    def execute(self, selection, *, work_id):
        current = self._read(selection, work_id)
        if current.state != "prepared":
            return current
        with self._lock:
            if self._closed:
                raise PermissionError("managed dispatcher is closed")
        approval = self._authorize(current.prepared, selection.context)
        if type(approval) is not GrantedApprovalEvidence:
            raise PermissionError("exact host dispatch approval required")
        approval.__post_init__()
        if (
            approval.tool != "workspace_run"
            or approval.surface != "app-control"
            or approval.call_digest != dispatch_approval_digest(current.prepared)
            or approval.expires_at > current.prepared.expires_at
            or approval.expires_at <= time.time()
        ):
            raise PermissionError("host dispatch approval expired")
        if not self._slots.acquire(blocking=False):
            raise CapacityExceeded("managed execution capacity unavailable")
        lease, entry = None, None
        submitting = False
        try:
            lease = self.authority.retain_selection(selection)
            with self._lock:
                if self._closed or len(self._runs) >= self._capacity:
                    raise CapacityExceeded(
                        "retained managed execution capacity unavailable"
                    )
                self._submitting += 1
                submitting = True
            dispatch_id = uuid.uuid4().hex
            try:

                def admit(tx):
                    if approval.expires_at <= time.time():
                        raise PermissionError(
                            "host dispatch approval expired before admission"
                        )
                    return tx.admit_work(
                        **self._scope(selection, work_id),
                        expected_revision=1,
                        dispatch_id=dispatch_id,
                        process_incarnation=self._incarnation,
                    )

                admission = self.authority.work_atomic(
                    selection, selection.context, admit
                )
            except BaseException:
                # A committed-but-lost response is observational, never a launch grant.
                observed = self._read(selection, work_id)
                if observed.state == "prepared":
                    raise
                if (
                    observed.dispatch_id == dispatch_id
                    and observed.process_incarnation == self._incarnation
                ):
                    self._unknown(selection, observed, "CALLBACK_OUTCOME_UNKNOWN")
                return self._read(selection, work_id)
            if not admission.newly_admitted:
                return admission.record
            entry = _Run(selection, lease, admission.record)
            with self._lock:
                self._runs[work_id] = entry
            try:
                future = self._executor.submit(self._run, entry)
                entry.future = future
                future.add_done_callback(lambda result: self._finished(entry, result))
            except BaseException:
                with entry.cleanup_lock:
                    entry.submission_failed = True
                    if entry.callback_exited:
                        entry.finished = True
                # submit can have queued work before failing: keep ownership until
                # an actual callback completion proves no invocation remains.
                self._unknown(selection, admission.record, "CALLBACK_OUTCOME_UNKNOWN")
                if entry.finished:
                    self._cleanup(entry)
                raise OutcomeUnknown("managed submission outcome unknown") from None
            return admission.record
        finally:
            try:
                if entry is None:
                    if lease is not None:
                        self.authority.release_retained(lease)
                    self._slots.release()
            finally:
                if submitting:
                    with self._condition:
                        self._submitting -= 1
                        self._condition.notify_all()

    def _transition(self, entry, operation, **fields):
        record = self._read(entry.selection, entry.record.prepared.work_id)
        if (
            record.dispatch_id != entry.record.dispatch_id
            or record.process_incarnation != self._incarnation
        ):
            raise PermissionError("exact process dispatch identity changed")
        updated = self.authority.work_atomic(
            entry.selection,
            entry.selection.context,
            lambda tx: getattr(tx, operation)(
                **self._scope(entry.selection, record.prepared.work_id),
                expected_revision=record.revision,
                dispatch_id=record.dispatch_id,
                process_incarnation=record.process_incarnation,
                **fields,
            ),
        )
        entry.record = updated
        return updated

    def _unknown(self, selection, record, code):
        try:
            current = self._read(selection, record.prepared.work_id)
            if (
                current.state
                not in ("admitted", "run_binding", "running", "verification_pending")
                or current.dispatch_id != record.dispatch_id
                or current.process_incarnation != record.process_incarnation
            ):
                return
            interruption = WorkInterruption(
                current.state,
                code,
                canonical_digest(
                    dict(
                        work=current.prepared.work_id,
                        dispatch=current.dispatch_id,
                        revision=current.revision,
                        code=code,
                    )
                ),
            )
            self.authority.work_atomic(
                selection,
                selection.context,
                lambda tx: tx.mark_work_unknown(
                    **self._scope(selection, current.prepared.work_id),
                    expected_revision=current.revision,
                    dispatch_id=current.dispatch_id,
                    process_incarnation=current.process_incarnation,
                    interruption=interruption,
                ),
            )
        except BaseException:
            # Revoked/unknown persistence cannot be repaired with stored identities.
            pass

    def _run(self, entry):
        stage = "CALLBACK_OUTCOME_UNKNOWN"
        try:
            current = self._read(entry.selection, entry.record.prepared.work_id)
            if current != entry.record or current.state != "admitted":
                raise PermissionError("dispatch is no longer executable")
            lifetime = self._factory(entry.selection)
            if type(lifetime) is not ManagedConversationLifetime:
                raise TypeError("actual managed conversation lifetime required")
            entry.lifetime = lifetime

            def factory(controller, application):
                nonlocal stage
                stage = "HOST_LINK_OUTCOME_UNKNOWN"
                if self.application is not None and application is not self.application:
                    raise PermissionError("owned work Application identity changed")
                self._transition(entry, "bind_work_run", run_id=controller.run_id)
                view = lifetime.factory(controller, application)
                turn = view.turn_link()
                self._transition(entry, "bind_work_host", host_turn=turn)
                stage = "CALLBACK_OUTCOME_UNKNOWN"
                return view

            output = self.workbench.execute_prepared_workbench(
                current.prepared.plan,
                admitted_context=entry.selection.context,
                managed_factory=factory,
            )
            stage = "FINAL_PUBLICATION_UNKNOWN"
            finalized = lifetime.finalize_result_with_receipt(output)
            if (
                type(finalized) is not FinalizedHostResult
                or finalized.receipt.turn != entry.record.host_turn
            ):
                raise PermissionError("exact finalized host result required")
            from .managed_terminal_eligibility import ManagedTerminalEligibility

            eligibility = self._eligibility(lifetime, entry.record.host_turn, finalized)
            if (
                type(eligibility) is not ManagedTerminalEligibility
                or eligibility.evidence.result != finalized
                or eligibility.evidence.result.receipt.turn != entry.record.host_turn
            ):
                raise PermissionError("trusted exact terminal eligibility required")
            if eligibility.eligible is True:
                completion = WorkCompletionEvidence(
                    eligibility.phase,
                    pending_identity=eligibility.pending_identity,
                    publication_receipt=(
                        eligibility.published.receipt
                        if eligibility.published is not None
                        else None
                    ),
                )
                self._transition(
                    entry,
                    "record_work_terminal",
                    terminal=finalized.receipt,
                    completion=completion,
                )
            elif (
                eligibility.eligible is False
                and eligibility.phase == "approval_pending"
            ):
                self._transition(
                    entry,
                    "record_work_verification_pending",
                    pending=WorkVerificationPending(
                        eligibility.pending_identity,
                        eligibility.pending_approval,
                        finalized.receipt,
                    ),
                )
            else:
                self._unknown(entry.selection, entry.record, stage)
        except BaseException:
            self._unknown(entry.selection, entry.record, stage)
        finally:
            with entry.cleanup_lock:
                entry.callback_exited = True
                if entry.submission_failed:
                    entry.finished = True
            if entry.finished:
                self._cleanup(entry)

    def _finished(self, entry, future):
        with entry.cleanup_lock:
            entry.finished = True
        if future.cancelled() or future.exception() is not None:
            self._unknown(entry.selection, entry.record, "CALLBACK_OUTCOME_UNKNOWN")
        self._cleanup(entry)

    def _cleanup(self, entry):
        with entry.cleanup_lock:
            if not entry.finished:
                raise PermissionError("worker completion is not proven")
            try:
                if entry.lifetime is not None:
                    entry.lifetime.close()
                if not entry.lease_released:
                    self.authority.release_retained(entry.lease)
                    entry.lease_released = True
                if not entry.selection_released:
                    self.authority.release_selection(entry.selection)
                    entry.selection_released = True
            except BaseException:
                return False
            with self._lock:
                key = entry.record.prepared.work_id
                if self._runs.get(key) is entry:
                    del self._runs[key]
                    self._slots.release()
            return True

    def retry_cleanup(self, selection, *, work_id):
        self._read(selection, work_id)
        with self._lock:
            entry = self._runs.get(work_id)
        if entry is None:
            return True
        if (
            entry.record.prepared.binding != selection.binding
            or entry.record.prepared.selection != selection.slot
        ):
            raise PermissionError("cleanup selection mismatch")
        return self._cleanup(entry)

    def close(self):
        with self._condition:
            self._closed = True
            while self._submitting:
                self._condition.wait()
        self._executor.shutdown(wait=True, cancel_futures=False)
        with self._lock:
            entries = tuple(self._runs.values())
        for entry in entries:
            if entry.finished:
                self._cleanup(entry)
        with self._lock:
            if self._runs:
                raise PermissionError("managed work cleanup remains unresolved")
