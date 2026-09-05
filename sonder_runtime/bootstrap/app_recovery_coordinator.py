"""One explicitly owned app recovery attempt; never dispatches a model turn."""

from dataclasses import dataclass, field
from threading import RLock

from ..application.ports.app_control import CommandKey, CommandConflict, identifier
from ..application.ports.app_managed_work import AppWorkRecord, WorkCompletionEvidence
from ..application.ports.lane_continuation import (
    PendingApprovalEvidence,
    VerificationApprovalPending,
)
from .app_work_recovery import AppWorkRecoveryHistory
from .managed_standalone import ManagedStandaloneRecovery, PreparedManagedReattachment


@dataclass(frozen=True)
class PreparedAppRecovery:
    work: AppWorkRecord
    attachment: PreparedManagedReattachment
    completion_command_id: str
    issuer: object = field(repr=False, compare=False)


@dataclass(frozen=True)
class AppRecoveryView:
    work: AppWorkRecord
    phase: str
    code: str
    approval: PendingApprovalEvidence | None = None


class AppWorkRecoveryAttempt:
    """Caller transfers its selection and must retain this handle until close succeeds.

    The hosting runtime must bound and own attempts. The factories are private
    bootstrap inputs, never request callbacks or model-supplied functions.
    """

    def __init__(
        self,
        *,
        authority,
        selection,
        application,
        recovery_factory,
        verifier_factory,
        approve_attachment,
        approve_verification,
        private_paths,
        model_writable_roots
    ):
        if application is None or not all(
            callable(value)
            for value in (
                recovery_factory,
                verifier_factory,
                approve_attachment,
                approve_verification,
                private_paths,
                model_writable_roots,
            )
        ):
            raise TypeError("private recovery and verifier composition required")
        self._authority, self._selection, self._application = (
            authority,
            selection,
            application,
        )
        self._factory, self._verifier_factory = recovery_factory, verifier_factory
        self._attachment_gate, self._verification_gate = (
            approve_attachment,
            approve_verification,
        )
        self._private_paths, self._model_roots = private_paths, model_writable_roots
        self._history = AppWorkRecoveryHistory(authority)
        self._lock, self._issuer = RLock(), object()
        self._prepared = self._recovery = self._session = None
        self._inputs = None
        self._closed = self._closing = False
        self._lease = authority.retain_selection(selection)

    def _live(self):
        if self._closed or self._closing:
            raise PermissionError("recovery attempt is closed or awaiting cleanup")
        self._authority.work_atomic(
            self._selection, self._selection.context, lambda tx: None
        )

    def prepare(self, *, work_id, attachment_command_id, completion_command_id):
        with self._lock:
            self._live()
            for value in (work_id, attachment_command_id, completion_command_id):
                identifier(value)
            inputs = (work_id, attachment_command_id, completion_command_id)
            if self._inputs is not None and self._inputs != inputs:
                raise CommandConflict("recovery attempt identity is immutable")
            if self._prepared is not None:
                self._current(self._prepared)
                return self._prepared
            work = self._history.inspect(self._selection, work_id=work_id)
            if (
                work is None
                or work.state not in ("verification_pending", "unknown")
                or work.verification_pending is None
            ):
                raise CommandConflict("exact retained pending work required")
            self._inputs = inputs
            if self._recovery is None:
                recovery = self._factory(self._selection, work)
                if (
                    type(recovery) is not ManagedStandaloneRecovery
                    or recovery._application is not self._application
                    or recovery._host.managed_authority is not self._authority
                    or recovery._host.authority_subject is not self._selection
                    or recovery._context is not self._selection.context
                    or recovery._attachment_gate is not self._attachment_gate
                    or recovery._verification_gate is not self._verification_gate
                    or recovery._private_paths is not self._private_paths
                    or recovery._model_roots is not self._model_roots
                    or recovery._host_id != work.prepared.binding.canonical_host_id
                ):
                    raise PermissionError(
                        "exact private account recovery composition required"
                    )
                self._recovery = recovery
            attachment = self._recovery.prepare(
                work.verification_pending.identity.continuation_id,
                command_id=attachment_command_id,
            )
            if (
                attachment.attachment.parent_session_id
                != work.host_turn.parent_session_id
                or attachment.attachment.host_conversation_id
                != work.host_turn.host_conversation_id
            ):
                raise PermissionError("reattachment does not match original work")
            self._prepared = PreparedAppRecovery(
                work, attachment, completion_command_id, self._issuer
            )
            return self._prepared

    def _current(self, prepared):
        self._live()
        if (
            type(prepared) is not PreparedAppRecovery
            or prepared is not self._prepared
            or prepared.issuer is not self._issuer
        ):
            raise PermissionError("exact private prepared recovery required")
        current = self._history.inspect(
            self._selection, work_id=prepared.work.prepared.work_id
        )
        if (
            current is None
            or current.prepared != prepared.work.prepared
            or current.host_turn != prepared.work.host_turn
            or current.verification_pending != prepared.work.verification_pending
            or current.dispatch_id != prepared.work.dispatch_id
            or current.process_incarnation != prepared.work.process_incarnation
        ):
            raise CommandConflict("original recovery work changed")
        if current != prepared.work and current.state != "terminal":
            raise CommandConflict("recovery work revision changed")
        return current

    def attach(self, prepared):
        with self._lock:
            current = self._current(prepared)
            if current.state == "terminal":
                raise CommandConflict("completed work requires observation only")
            if self._session is None:
                try:
                    self._session = self._recovery.execute(prepared.attachment)
                except VerificationApprovalPending as pending:
                    return AppRecoveryView(
                        current,
                        "attachment_pending",
                        "APPROVAL_PENDING",
                        pending.evidence,
                    )
            self._validate_original(prepared)
            return AppRecoveryView(
                current, "attached", "EXPLICIT_VERIFICATION_RESUME_REQUIRED"
            )

    def _validate_original(self, prepared):
        if self._session is None:
            raise PermissionError("explicit host attachment required")
        self._session.require_current()
        pending = prepared.work.verification_pending
        if self._session._bound.pending_verification() != pending.identity:
            raise PermissionError("attached pending identity changed")
        evidence = self._session.final_evidence(prepared.work.host_turn)
        if evidence.result.receipt != pending.original_terminal:
            raise PermissionError("attached original terminal changed")

    def resume(self, prepared):
        with self._lock:
            current = self._current(prepared)
            if current.state == "terminal":
                # The store requires our exact already-committed command receipt;
                # its terminal replay performs no mutation. Never revisit gates,
                # verifier jobs or publication when this attempt is complete.
                result = self._complete(prepared, current.terminal, current.completion)
                code = (
                    "RECOVERED_CERTIFIED"
                    if result.completion.phase == "certified_after_return"
                    else "CERTIFIED"
                )
                return AppRecoveryView(result, "terminal", code)
            self._validate_original(prepared)
            # Existing verifier has its own durable claim/approval guards; status
            # reads never reach this explicit operation.
            self._session.resume_pending_verification(
                prepared.work.verification_pending.identity,
                verifier_factory=self._verifier_factory,
                publish=False,
            )
            eligible = self._session.terminal_eligibility(
                prepared.work.host_turn,
                verifier_factory=self._verifier_factory,
            )
            if not eligible.eligible:
                return AppRecoveryView(
                    current, eligible.phase, eligible.code, eligible.pending_approval
                )
            if (
                eligible.phase not in ("certified", "certified_after_return")
                or eligible.published is None
            ):
                raise PermissionError("fresh recovered certification required")
            if (
                eligible.evidence.result.receipt
                != prepared.work.verification_pending.original_terminal
            ):
                raise PermissionError("fresh recovery terminal differs from original")
            completion = WorkCompletionEvidence(
                eligible.phase, eligible.pending_identity, eligible.published.receipt
            )
            result = self._complete(
                prepared, eligible.evidence.result.receipt, completion
            )
            return AppRecoveryView(result, "terminal", eligible.code)

    def _complete(self, prepared, terminal, completion):
        selected = self._selection
        return self._authority.work_atomic(
            selected,
            selected.context,
            lambda tx: tx.complete_recovery_work(
                CommandKey(
                    selected.binding.principal_id,
                    "control:" + selected.control.control_session_id,
                    prepared.completion_command_id,
                ),
                principal_id=selected.binding.principal_id,
                control_session_id=selected.control.control_session_id,
                binding_id=selected.binding.binding_id,
                binding_revision=selected.binding.revision,
                selection_id=selected.slot.selection_id,
                epoch=selected.slot.epoch,
                work_id=prepared.work.prepared.work_id,
                expected_revision=prepared.work.revision,
                terminal=terminal,
                completion=completion,
            ),
        )

    def inspect(self, prepared):
        with self._lock:
            return self._current(prepared)

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closing = True
            if self._session is not None:
                self._session.close()
                self._session = None
            if self._lease is not None:
                self._authority.release_retained(self._lease)
                self._lease = None
            self._authority.release_selection(self._selection)
            self._closed = True
