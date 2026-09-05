"""Private composition for a persisted, locally selected REPL work turn."""

import hashlib
import json
from pathlib import Path
import time
import uuid
from dataclasses import dataclass
from types import SimpleNamespace

from ..adapters.filesystem.file_ops import managed_root_scope
from ..adapters.host_terminal_projection import TerminalProjectionCodec
from ..adapters.persistence.terminal_output import SQLiteTerminalOutputStore
from ..adapters.security.control_plane_paths import (
    ControlPlanePaths, control_plane_scope, live_control_plane_inventory,
)
from ..adapters.security.continuation_approval import ContinuationApprovalBridge
from ..application.agents.lane_continuation import LaneContinuationService
from ..application.context import local_owner_context
from ..interfaces.standalone_agent_lanes import managed_controller_factory_scope
from ..platform import paths
from .managed_standalone import ManagedStandaloneSession, ManagedStandaloneRecovery
from ..application.ports.lane_continuation import VerificationApprovalPending
from .repl_host_selection import ReplHostPolicy, ReplHostSelectionAdapter
from .managed_conversation import ManagedConversationLifetime, ReplConversationSlot


@dataclass(frozen=True)
class ReplRecoveryRequest:
    continuation_id: str
    command_id: str

    def __post_init__(self):
        for value in (self.continuation_id, self.command_id):
            if not isinstance(value, str) or not 1 <= len(value) <= 128 or not all(
                    char.isascii() and (char.isalnum() or char in '-_') for char in value):
                raise ValueError('bounded recovery identities required')


@dataclass(frozen=True)
class ReplRecoveryResult:
    code: str
    approval_call_id: str = ''
    output: str = ''


def run_managed_repl_work(*, application, session_id, project, get_session,
                          run, permission_engine, additional_paths, ledger=None,
                          recovery_cursor=None, recovery_request=None, verifier_factory=None,
                          conversation_slot=None, conversation_source=None):
    """Compose only from trusted host callbacks, never public tool arguments.

    ``additional_paths`` must enumerate constructor-owned private state before
    any lazy provider is initialized. The entry point retains no parent token.
    """
    if not callable(additional_paths) or not callable(run):
        raise PermissionError('trusted REPL composition callbacks required')
    if conversation_slot is not None and recovery_cursor is None and recovery_request is None:
        if type(conversation_slot) is not ReplConversationSlot or not isinstance(conversation_source, str):
            raise PermissionError('private launcher conversation slot required')
        identity = (id(application), session_id, str(Path(project).resolve()),
                    conversation_source, str(getattr(ledger, 'path', '')))
        if conversation_slot.select(identity):
            return conversation_slot.run(run)
    if recovery_cursor is not None and (
            type(recovery_cursor) is not int or not 0 <= recovery_cursor < 2**63):
        raise ValueError('bounded recovery cursor required')
    if recovery_request is not None:
        if type(recovery_request) is not ReplRecoveryRequest or recovery_cursor is not None or not callable(verifier_factory):
            raise ValueError('exact private recovery request and verifier required')
        recovery_request.__post_init__()
    selected = Path(project).resolve() if project else None
    if selected is None or not selected.is_dir():
        raise PermissionError('select an existing project before managed work')

    def model_roots():
        configured = tuple(Path(value).resolve()
                           for value in application.config.state.workspace_roots)
        if not 1 <= len(configured) <= 256 or any(not root.is_dir() for root in configured):
            raise PermissionError('complete live model workspace inventory unavailable')
        return configured

    def roots():
        configured = model_roots()
        result = tuple(sorted({selected if selected.is_relative_to(root) else root
                               for root in configured if root.is_dir() and
                               (selected.is_relative_to(root) or root.is_relative_to(selected))}))
        if not 1 <= len(result) <= 16:
            raise PermissionError('selected project has no bounded current workspace grant')
        return result

    output_root = (paths.default_home() / 'terminal-output').resolve()
    output_paths = ControlPlanePaths(owned_directories=(output_root,))

    def inventory():
        # Scope adds constructor provenance while shared resolvers remain live.
        with control_plane_scope(additional_paths()), control_plane_scope(output_paths):
            result = live_control_plane_inventory()
        result.require_disjoint(model_roots())
        return result

    inventory()  # Must precede output-directory and lane-store initialization.
    lanes = application.agent_lanes()
    expiry = time.time() + 3600
    original_roots = roots()
    original_tools = tuple(sorted(lanes.allowed_tools))
    policy_digest = hashlib.sha256(json.dumps({
        'session': session_id, 'roots': [str(root) for root in original_roots],
        'tools': original_tools,
        'remote': application.config.ollama.allow_remote,
    }, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    context = local_owner_context(
        correlation_id='repl-work-' + uuid.uuid4().hex, source='repl',
        workspace_roots=original_roots, timeout_seconds=3600,
        remote_ollama_allowed=application.config.ollama.allow_remote,
    )

    def policy(current, exact_session):
        inventory()
        if exact_session != session_id:
            raise PermissionError('REPL selection changed')
        if current.remote_ollama_allowed and not application.config.ollama.allow_remote:
            raise PermissionError('configured remote inference grant was removed')
        return ReplHostPolicy('repl-policy-' + policy_digest, 1, expiry,
                              tuple(str(root) for root in roots()),
                              tuple(sorted(lanes.allowed_tools)))

    selector = ReplHostSelectionAdapter(
        get_session=get_session, find_session=lambda query: query,
        touch_session=lambda value: None, policy=policy,
    )
    selection = selector.select_exact(session_id, context)
    active = []
    consumed = False

    def current_context():
        selector.authorize(context, selection.host_conversation_id)
        inventory()
        return active[0].context if active else context

    def decide(tool, **kwargs):
        current_context()
        return permission_engine.decide(tool, interactive=False, **kwargs)

    def approve(prepared, admitted_context):
        current_context()
        selector.authorize(admitted_context, selection.host_conversation_id)
        return bridge.authorize('workspace_run', prepared.approval_payload(),
                                surface='agent', expires_at=min(
                                    expiry, getattr(prepared, 'expires_at', expiry),
                                    time.time() + admitted_context.remaining_seconds))

    def factory(controller, current_application):
        nonlocal consumed
        if current_application is not application or consumed:
            raise PermissionError('managed REPL factory already consumed or application changed')
        consumed = True
        current_context()
        output_store = SQLiteTerminalOutputStore(output_root, model_writable_roots=model_roots)
        codec = TerminalProjectionCodec(output_store=output_store, output_context=current_context)
        host = LaneContinuationService(lanes, authorize_host=selector.authorize,
                                       projection_codec=codec, model_writable_roots=model_roots)
        session = ManagedStandaloneSession(
            controller=controller, application=application, host=host, context=context,
            host_conversation_id=selection.host_conversation_id,
            private_paths=lambda: inventory().admission_directories,
            model_writable_roots=model_roots, approve=approve,
        )
        active.append(session)
        return session

    if conversation_slot is not None and recovery_cursor is None and recovery_request is None:
        bridge = ContinuationApprovalBridge(ledger=ledger or permission_engine.approval_ledger().pinned(),
                                            decide=decide, digest_call=permission_engine.call_digest)
        conversation = ManagedConversationLifetime(application=application,
            session_factory=factory, require_current=current_context)

        def invoke(current_run):
            with selector.scope(selection, context), control_plane_scope(additional_paths()), \
                    control_plane_scope(output_paths), managed_root_scope(lambda: current_context().workspace_roots), \
                    managed_controller_factory_scope(conversation.factory):
                return conversation.finalize_result(current_run())

        def close():
            conversation.close()
            selector.clear()

        conversation_slot.install(invoke, close)
        return conversation_slot.run(run)

    with selector.scope(selection, context), control_plane_scope(additional_paths()), \
            control_plane_scope(output_paths), managed_root_scope(lambda: current_context().workspace_roots):
        if recovery_cursor is not None:
            try:
                current_context()
                host = LaneContinuationService(lanes, authorize_host=selector.authorize,
                                               model_writable_roots=model_roots)
                return host.recovery_page(context, cursor=recovery_cursor, limit=16,
                                           host_conversation_id=selection.host_conversation_id)
            finally:
                selector.clear()
        # Pin the real ledger after private path preflight, before any gate call.
        bridge = ContinuationApprovalBridge(ledger=ledger or permission_engine.approval_ledger().pinned(),
                                            decide=decide, digest_call=permission_engine.call_digest)
        try:
            if recovery_request is not None:
                host = LaneContinuationService(lanes, authorize_host=selector.authorize,
                                               model_writable_roots=model_roots)
                cursor, original = 0, None
                while True:
                    page = host.recovery_page(context, cursor=cursor, limit=128,
                                               host_conversation_id=selection.host_conversation_id)
                    original = next((item for item in page.items if item.continuation_id == recovery_request.continuation_id), None)
                    if original is not None or not page.has_more:
                        break
                    cursor = page.next_cursor
                if original is None or original.pending_identity is None:
                    return ReplRecoveryResult('ORIGINAL_VERIFICATION_UNAVAILABLE')
                current_context()
                output_store = SQLiteTerminalOutputStore(output_root, model_writable_roots=model_roots)
                host.projection_codec = TerminalProjectionCodec(output_store=output_store, output_context=current_context)
                coordinator = ManagedStandaloneRecovery(
                    controller=SimpleNamespace(run_id='repl-recovery-' + uuid.uuid4().hex),
                    application=application, host=host, context=context,
                    host_conversation_id=selection.host_conversation_id,
                    private_paths=lambda: inventory().admission_directories,
                    model_writable_roots=model_roots,
                    approve_attachment=approve, approve_verification=approve,
                )
                prepared = coordinator.prepare(recovery_request.continuation_id,
                                                command_id=recovery_request.command_id)
                try:
                    session = coordinator.execute(prepared)
                except VerificationApprovalPending as pending:
                    return ReplRecoveryResult('ATTACHMENT_APPROVAL_PENDING', pending.evidence.call_id)
                active.append(session)
                verdict = session.resume_pending_verification(original.pending_identity,
                                                               verifier_factory=verifier_factory)
                if session.published_terminal is not None:
                    return ReplRecoveryResult('VERIFIED' if verdict.valid else verdict.code,
                                               output=session.published_terminal.output)
                return ReplRecoveryResult(verdict.code)
            with managed_controller_factory_scope(factory):
                return run()
        finally:
            selector.clear()
            for session in active:
                session.close()
