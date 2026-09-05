"""Private managed session consumed by the standalone controller hook."""

from dataclasses import dataclass, field
import json

from ..interfaces.standalone_agent_lanes import PreparedLaneCommand, HostTerminalDraft
from ..adapters.agent_terminal_evidence import HostObservationLedger
from ..adapters.host_terminal_projection import _FAILURE_MARKERS
from ..application.ports.delegated_verification import VerificationVerdict
from ..application.ports.lane_continuation import ProjectionBinding
from .standalone_continuation import HostContinuationAdmission, HostTerminalPublisher


@dataclass(frozen=True)
class _HostControl:
    action: str
    encoded: str
    issuer: object = field(repr=False, compare=False)


class ManagedStandaloneSession:
    """Register once and retain only the bound continuation, never its bearer.

    ``host`` must be a fresh service composed for this verified host selection.
    Inventory and approval callbacks are private bootstrap inputs.
    """

    def __init__(self, *, controller, application, host, context,
                 host_conversation_id, private_paths, model_writable_roots, approve):
        if host.command_codec is not None or host.terminal_result_codec is not None:
            raise PermissionError('managed session requires an unshared host service')
        if host.projection_codec is None or not callable(approve):
            raise PermissionError('managed terminal codec and approval bridge required')
        # Reject private-state exposure before allocating a parent at all.
        private = HostContinuationAdmission._paths(private_paths())
        roots = (HostContinuationAdmission._paths(model_writable_roots())
                 | HostContinuationAdmission._paths(context.workspace_roots))
        if any(p.is_relative_to(root) or root.is_relative_to(p)
               for p in private for root in roots):
            raise PermissionError('model workspace overlaps private control-plane state')
        self._controller = controller
        self._application = application
        self._host = host
        self._host_id = host_conversation_id
        self._approve = approve
        self._issuer = object()
        self._closed = False
        self._verifier = self._publisher = None
        self.published_terminal = None
        parent = host.lanes.open_model_parent(context)
        self._bound = host.register_parent(parent['parent_session_id'],
            parent['parent_token'], host_conversation_id, context=context,
            command_id=controller.run_id + '-register')
        self.parent_session_id = parent['parent_session_id']
        del parent
        try:
            self._admission = HostContinuationAdmission(self._bound, context,
                private_paths=private_paths, model_writable_roots=model_writable_roots)
        except BaseException:
            self._bound.close()
            raise
        host.command_codec = self

    @property
    def context(self):
        self.require_current()
        return self._admission.context

    def require_current(self):
        if self._closed:
            raise PermissionError('managed host session is closed')
        self._admission.require_current()

    def decode_command(self, prepared):
        self.require_current()
        if type(prepared) is _HostControl and prepared.issuer is self._issuer:
            return prepared.action, json.loads(prepared.encoded)
        if type(prepared) is not PreparedLaneCommand or prepared.owner is not self._controller:
            raise PermissionError('exact controller-issued command required')
        if len(prepared.encoded.encode()) > 65536:
            raise ValueError('managed command exceeds bound')
        safe = prepared.approval_arguments()
        if (not isinstance(safe, dict) or set(safe) != {'action', 'payload',
                'standalone_run_id', 'principal_id', 'workspace_roots'}
                or safe['standalone_run_id'] != self._controller.run_id
                or safe['principal_id'] != self.context.principal_id
                or safe['workspace_roots'] != [str(p) for p in self.context.workspace_roots]):
            raise PermissionError('approved managed command binding changed')
        return safe['action'], safe['payload']

    def dispatch(self, prepared):
        self.require_current()
        return self._bound.dispatch(prepared)

    def _control(self, action, payload):
        return self.dispatch(_HostControl(action,
            json.dumps(payload, allow_nan=False), self._issuer))

    def report_metadata(self):
        children = self._control('list', {'limit': 100})['lanes']
        return {'standalone_run_id': self._controller.run_id,
            'parent_session_id': self.parent_session_id,
            'continuation_id': self._bound.continuation_id,
            'verification': 'delegated-work-verification-required',
            'children': [{key: child[key] for key in ('id', 'revision', 'status')}
                         for child in children]}

    def request_cancel(self):
        self.require_current()
        children = self._control('list', {'limit': 100})['lanes']
        for child in children:
            self._control('cancel', {'lane_id': child['id'],
                'command_id': self._controller.run_id + '-cancel-' + child['id'],
                'reason': 'standalone controller cancelled'})

    def _approve_verification(self, prepared, context):
        self._admission.require_current(context=context)
        return self._approve(prepared, context)

    def verify_delegated(self, draft, *, verifier_factory):
        self.published_terminal = None
        self.require_current()
        if type(draft) is not HostTerminalDraft:
            raise PermissionError('original host terminal draft required')
        ledger = HostObservationLedger.restore(draft.ledger_bytes)
        if (draft.terminal_class != 'NORMAL' or draft.blockers
                or draft.output.lstrip().startswith(tuple(m for m, _ in _FAILURE_MARKERS))
                or not ledger.resolve().parent_effects_valid):
            return VerificationVerdict(False, 'ORIGINAL_PARENT_EVIDENCE_FAILED')
        if self._verifier is None:
            self._verifier = verifier_factory(self._application, self._host.lanes)
            self._publisher = HostTerminalPublisher(bound=self._bound,
                verifier=self._verifier, original_codec=self._host.projection_codec,
                require_current=self.require_current)
            self._host.terminal_result_codec = self._publisher.codec
        identity = self._bound.pending_verification()
        if identity is None:
            prepared = self._bound.prepare_verification(self._verifier,
                command_id=self._controller.run_id + '-verify')
            try:
                binding = ProjectionBinding(self._bound.continuation_id,
                    self.context.principal_id, self._controller.run_id, self._host_id,
                    prepared.parent_session_id, prepared.parent_grant_revision,
                    prepared.verification_id, prepared.bundle_digest, prepared.roots, 1)
                original = self._host.projection_codec.capture(binding=binding,
                    ledger=ledger, output=draft.output, terminal_class=draft.terminal_class,
                    blockers=draft.blockers,
                    terminal_receipt_id=self._controller.run_id + '-terminal')
                identity = self._bound.link_pending_verification(self._verifier,
                    prepared, original)
            except BaseException:
                # No approval/check is reachable before successful linkage.
                # Reconciliation releases an admitted no-job barrier without
                # inventing success or replaying uncertain effects.
                self._bound.verification_view(self._verifier,
                    prepared.verification_id, action='reconcile')
                raise
        else:
            prepared = self._bound.prepared_verification(identity)
            original = self._bound.terminal_projection(identity)
            if (original.output != draft.output or original.ledger_bytes != draft.ledger_bytes
                    or original.terminal_class != draft.terminal_class
                    or original.blockers != draft.blockers):
                raise PermissionError('original linked terminal draft is immutable')
        self._bound.execute_verification(self._verifier, prepared,
            approve=self._approve_verification)
        verdict = self._bound.verification_view(self._verifier,
            prepared.verification_id, action='validate')
        if verdict.valid is not True:
            return verdict
        self.published_terminal = self._publisher.publish()
        return self.published_terminal.verdict

    def close(self):
        self._closed = True
        self.published_terminal = None
        self._bound.close()
