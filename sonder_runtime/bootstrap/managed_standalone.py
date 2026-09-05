"""Private managed session consumed by the standalone controller hook."""

from dataclasses import dataclass, field
import json
from threading import RLock

from ..interfaces.standalone_agent_lanes import PreparedLaneCommand, HostTerminalDraft
from ..adapters.agent_terminal_evidence import HostObservationLedger
from ..adapters.host_terminal_projection import _FAILURE_MARKERS
from ..application.ports.delegated_verification import (
    PreparedVerification,
    VerificationVerdict,
)
from ..application.ports.lane_continuation import (
    PendingVerificationIdentity,
    PreparedReattachment,
    ProjectionBinding,
    VerificationApprovalPending,
)
from .standalone_continuation import HostContinuationAdmission, HostTerminalPublisher


@dataclass(frozen=True)
class _HostControl:
    action: str
    encoded: str
    issuer: object = field(repr=False, compare=False)


@dataclass(frozen=True)
class PreparedManagedReattachment:
    attachment: PreparedReattachment
    issuer: object = field(repr=False, compare=False)

    def approval_payload(self):
        return self.attachment.approval_payload()


@dataclass(frozen=True)
class ManagedVerificationRecovery:
    identity: PendingVerificationIdentity
    prepared: PreparedVerification
    phase: str
    code: str


class ManagedStandaloneRecovery:
    """Private explicit recovery coordinator; history IDs never grant authority."""

    def __init__(
        self,
        *,
        controller,
        application,
        host,
        context,
        host_conversation_id,
        private_paths,
        model_writable_roots,
        approve_attachment,
        approve_verification
    ):
        if (
            host.command_codec is not None
            or host.terminal_result_codec is not None
            or host.projection_codec is None
            or not all(
                callable(value)
                for value in (
                    private_paths,
                    model_writable_roots,
                    approve_attachment,
                    approve_verification,
                )
            )
        ):
            raise PermissionError("fresh private recovery composition required")
        self._controller, self._application, self._host = controller, application, host
        self._context, self._host_id = context, host_conversation_id
        self._private_paths, self._model_roots = private_paths, model_writable_roots
        self._attachment_gate, self._verification_gate = (
            approve_attachment,
            approve_verification,
        )
        self._issuer = object()
        self._lock = RLock()
        self._session = self._accepted = None
        self._unknown = False
        self._check()

    def _check(self):
        if self._context.expired or self._context.cancellation.cancelled:
            raise PermissionError("current host recovery context unavailable")
        private = HostContinuationAdmission._paths(self._private_paths())
        roots = HostContinuationAdmission._paths(
            self._model_roots()
        ) | HostContinuationAdmission._paths(self._context.workspace_roots)
        if any(
            path.is_relative_to(root) or root.is_relative_to(path)
            for path in private
            for root in roots
        ):
            raise PermissionError("model workspace overlaps private recovery state")

    def prepare(self, continuation_id, *, command_id):
        with self._lock:
            return self._prepare(continuation_id, command_id=command_id)

    def _prepare(self, continuation_id, *, command_id):
        self._check()
        if self._unknown or self._session is not None:
            raise PermissionError("recovery coordinator already attempted attachment")
        selection = self._host.select(continuation_id, self._context)
        attachment = self._host.prepare_reattachment(
            selection, self._context, command_id=command_id
        )
        if attachment.host_conversation_id != self._host_id:
            raise PermissionError(
                "selected host conversation does not own continuation"
            )
        return PreparedManagedReattachment(attachment, self._issuer)

    def _approve(self, prepared, context):
        self._check()
        result = self._attachment_gate(prepared, context)
        self._check()
        return result

    def execute(self, prepared):
        with self._lock:
            return self._execute(prepared)

    def _execute(self, prepared):
        if (
            type(prepared) is not PreparedManagedReattachment
            or prepared.issuer is not self._issuer
            or prepared.attachment.host_conversation_id != self._host_id
        ):
            raise PermissionError("private prepared recovery identity required")
        self._check()
        if self._session is not None:
            if prepared != self._accepted:
                raise PermissionError("recovery attachment identity changed")
            self._session.require_current()
            return self._session
        if self._unknown:
            raise PermissionError(
                "attachment outcome requires explicit recovery inspection"
            )
        self._unknown = True
        try:
            bound = self._host.execute_reattachment(
                prepared.attachment, self._context, approve=self._approve
            )
        except VerificationApprovalPending:
            self._unknown = False
            raise
        try:
            session = ManagedStandaloneSession._from_bound(
                controller=self._controller,
                application=self._application,
                host=self._host,
                context=self._context,
                host_conversation_id=self._host_id,
                bound=bound,
                parent_session_id=prepared.attachment.parent_session_id,
                private_paths=self._private_paths,
                model_writable_roots=self._model_roots,
                approve=self._verification_gate,
            )
        except BaseException:
            bound.close()
            raise
        self._session, self._accepted = session, prepared
        return session


class ManagedStandaloneSession:
    """Register once and retain only the bound continuation, never its bearer.

    ``host`` must be a fresh service composed for this verified host selection.
    Inventory and approval callbacks are private bootstrap inputs.
    """

    def __init__(
        self,
        *,
        controller,
        application,
        host,
        context,
        host_conversation_id,
        private_paths,
        model_writable_roots,
        approve
    ):
        if host.command_codec is not None or host.terminal_result_codec is not None:
            raise PermissionError("managed session requires an unshared host service")
        if host.projection_codec is None or not callable(approve):
            raise PermissionError("managed terminal codec and approval bridge required")
        # Reject private-state exposure before allocating a parent at all.
        private = HostContinuationAdmission._paths(private_paths())
        roots = HostContinuationAdmission._paths(
            model_writable_roots()
        ) | HostContinuationAdmission._paths(context.workspace_roots)
        if any(
            p.is_relative_to(root) or root.is_relative_to(p)
            for p in private
            for root in roots
        ):
            raise PermissionError(
                "model workspace overlaps private control-plane state"
            )
        self._controller = controller
        self._application = application
        self._host = host
        self._host_id = host_conversation_id
        self._approve = approve
        self._issuer = object()
        self._closed = False
        self._recovered = False
        self._verifier = self._publisher = None
        self.published_terminal = None
        if host.recovery_page(
            context, limit=1, host_conversation_id=host_conversation_id
        ).items:
            raise PermissionError(
                "registered conversation requires explicit reattachment"
            )
        parent = host.lanes.open_model_parent(context)
        self._bound = host.register_parent(
            parent["parent_session_id"],
            parent["parent_token"],
            host_conversation_id,
            context=context,
            command_id=controller.run_id + "-register",
        )
        self.parent_session_id = parent["parent_session_id"]
        del parent
        try:
            self._admission = HostContinuationAdmission(
                self._bound,
                context,
                private_paths=private_paths,
                model_writable_roots=model_writable_roots,
            )
        except BaseException:
            self._bound.close()
            raise
        host.command_codec = self

    @classmethod
    def _from_bound(
        cls,
        *,
        controller,
        application,
        host,
        context,
        host_conversation_id,
        parent_session_id,
        bound,
        private_paths,
        model_writable_roots,
        approve
    ):
        if host.command_codec is not None or host.terminal_result_codec is not None:
            raise PermissionError("recovery host service is already composed")
        session = cls.__new__(cls)
        session._controller, session._application, session._host = (
            controller,
            application,
            host,
        )
        session._host_id, session._approve = host_conversation_id, approve
        session._issuer = object()
        session._closed = False
        session._recovered = True
        session._verifier = session._publisher = None
        session.published_terminal = None
        session._bound = bound
        session.parent_session_id = parent_session_id
        session._admission = HostContinuationAdmission(
            bound,
            context,
            private_paths=private_paths,
            model_writable_roots=model_writable_roots,
        )
        identity = bound.pending_verification()
        if identity is not None and identity.parent_session_id != parent_session_id:
            raise PermissionError(
                "reattached parent differs from original pending identity"
            )
        host.command_codec = session
        return session

    def _compose_verifier(self, verifier_factory):
        self.require_current()
        if self._verifier is None:
            self._verifier = verifier_factory(self._application, self._host.lanes)
            self._publisher = HostTerminalPublisher(
                bound=self._bound,
                verifier=self._verifier,
                original_codec=self._host.projection_codec,
                require_current=self.require_current,
            )
            self._host.terminal_result_codec = self._publisher.codec

    def original_terminal_draft(self):
        self.require_current()
        identity = self._bound.pending_verification()
        if identity is None:
            raise PermissionError("persisted original terminal projection unavailable")
        original = self._bound.terminal_projection(identity)
        return HostTerminalDraft(
            original.ledger_bytes,
            original.output,
            original.terminal_class,
            original.blockers,
        )

    def recovery_verification(self, *, verifier_factory):
        self._compose_verifier(verifier_factory)
        identity = self._bound.pending_verification()
        if identity is None:
            return None
        prepared = self._bound.prepared_verification(identity)
        self._bound.terminal_projection(identity)
        view = self._bound.verification_view(
            self._verifier, identity.verification_id, action="inspect"
        )
        return ManagedVerificationRecovery(
            identity, prepared, view["state"], view.get("code", "")
        )

    def resume_pending_verification(self, identity, *, verifier_factory):
        self.published_terminal = None
        view = self.recovery_verification(verifier_factory=verifier_factory)
        if (
            view is None
            or type(identity) is not PendingVerificationIdentity
            or identity != view.identity
        ):
            raise PermissionError(
                "exact original pending verification identity required"
            )
        if view.phase == "approval_pending":
            self._verifier.resume_pending_approval(
                self._bound, identity, approve=self._approve_verification
            )
        elif view.phase != "certified":
            return VerificationVerdict(
                False, view.code or "RECOVERY_PHASE_NOT_RESUMABLE"
            )
        self.require_current()
        verdict = self._bound.verification_view(
            self._verifier, identity.verification_id, action="validate"
        )
        if not verdict.valid:
            return verdict
        self.published_terminal = self._publisher.publish()
        return self.published_terminal.verdict

    @property
    def context(self):
        self.require_current()
        return self._admission.context

    def require_current(self):
        if self._closed:
            raise PermissionError("managed host session is closed")
        guard = getattr(self, "_turn_guard", None)
        if guard is not None:
            guard()
        self._admission.require_current()

    def decode_command(self, prepared):
        self.require_current()
        if type(prepared) is _HostControl and prepared.issuer is self._issuer:
            return prepared.action, json.loads(prepared.encoded)
        if (
            type(prepared) is not PreparedLaneCommand
            or prepared.owner is not self._controller
        ):
            raise PermissionError("exact controller-issued command required")
        if len(prepared.encoded.encode()) > 65536:
            raise ValueError("managed command exceeds bound")
        safe = prepared.approval_arguments()
        if (
            not isinstance(safe, dict)
            or set(safe)
            != {
                "action",
                "payload",
                "standalone_run_id",
                "principal_id",
                "workspace_roots",
            }
            or safe["standalone_run_id"] != self._controller.run_id
            or safe["principal_id"] != self.context.principal_id
            or safe["workspace_roots"] != [str(p) for p in self.context.workspace_roots]
        ):
            raise PermissionError("approved managed command binding changed")
        return safe["action"], safe["payload"]

    def dispatch(self, prepared):
        self.require_current()
        return self._bound.dispatch(prepared)

    def _control(self, action, payload):
        return self.dispatch(
            _HostControl(action, json.dumps(payload, allow_nan=False), self._issuer)
        )

    def report_metadata(self):
        children = self._control("list", {"limit": 100})["lanes"]
        return {
            "standalone_run_id": self._controller.run_id,
            "parent_session_id": self.parent_session_id,
            "continuation_id": self._bound.continuation_id,
            "verification": "delegated-work-verification-required",
            "children": [
                {key: child[key] for key in ("id", "revision", "status")}
                for child in children
            ],
        }

    def request_cancel(self):
        self.require_current()
        children = self._control("list", {"limit": 100})["lanes"]
        for child in children:
            self._control(
                "cancel",
                {
                    "lane_id": child["id"],
                    "command_id": self._controller.run_id + "-cancel-" + child["id"],
                    "reason": "standalone controller cancelled",
                },
            )

    def _approve_verification(self, prepared, context):
        self._admission.require_current(context=context)
        return self._approve(prepared, context)

    def verify_delegated(self, draft, *, verifier_factory):
        if self._recovered:
            raise PermissionError(
                "recovered sessions require explicit original verification resume"
            )
        self.published_terminal = None
        self.require_current()
        if type(draft) is not HostTerminalDraft:
            raise PermissionError("original host terminal draft required")
        ledger = HostObservationLedger.restore(draft.ledger_bytes)
        if (
            draft.terminal_class != "NORMAL"
            or draft.blockers
            or draft.output.lstrip().startswith(tuple(m for m, _ in _FAILURE_MARKERS))
            or not ledger.resolve().parent_effects_valid
        ):
            return VerificationVerdict(False, "ORIGINAL_PARENT_EVIDENCE_FAILED")
        self._compose_verifier(verifier_factory)
        identity = self._bound.pending_verification()
        if identity is None:
            prepared = self._bound.prepare_verification(
                self._verifier, command_id=self._controller.run_id + "-verify"
            )
            try:
                binding = ProjectionBinding(
                    self._bound.continuation_id,
                    self.context.principal_id,
                    self._controller.run_id,
                    self._host_id,
                    prepared.parent_session_id,
                    prepared.parent_grant_revision,
                    prepared.verification_id,
                    prepared.bundle_digest,
                    prepared.roots,
                    1,
                )
                original = self._host.projection_codec.capture(
                    binding=binding,
                    ledger=ledger,
                    output=draft.output,
                    terminal_class=draft.terminal_class,
                    blockers=draft.blockers,
                    terminal_receipt_id=self._controller.run_id + "-terminal",
                )
                identity = self._bound.link_pending_verification(
                    self._verifier, prepared, original
                )
            except BaseException:
                # No approval/check is reachable before successful linkage.
                # Reconciliation releases an admitted no-job barrier without
                # inventing success or replaying uncertain effects.
                self._bound.verification_view(
                    self._verifier, prepared.verification_id, action="reconcile"
                )
                raise
        else:
            prepared = self._bound.prepared_verification(identity)
            original = self._bound.terminal_projection(identity)
            if (
                original.output != draft.output
                or original.ledger_bytes != draft.ledger_bytes
                or original.terminal_class != draft.terminal_class
                or original.blockers != draft.blockers
            ):
                raise PermissionError("original linked terminal draft is immutable")
        self._bound.execute_verification(
            self._verifier, prepared, approve=self._approve_verification
        )
        verdict = self._bound.verification_view(
            self._verifier, prepared.verification_id, action="validate"
        )
        if verdict.valid is not True:
            return verdict
        self.published_terminal = self._publisher.publish()
        return self.published_terminal.verdict

    def close(self):
        self._closed = True
        self.published_terminal = None
        self._bound.close()
