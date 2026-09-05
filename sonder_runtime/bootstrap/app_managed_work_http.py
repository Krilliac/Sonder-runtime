"""Owned private app work composition. Wire records never confer authority."""

from contextvars import ContextVar
from dataclasses import asdict
import hashlib
from pathlib import Path
import threading
import time
import uuid

from ..adapters.filesystem.file_ops import managed_root_scope
from ..adapters.host_terminal_projection import TerminalProjectionCodec
from ..adapters.persistence.terminal_output import SQLiteTerminalOutputStore
from ..adapters.security.account_admission import account_admission
from ..adapters.security.continuation_approval import (
    ApprovalOutcomeUnknown,
    ContinuationApprovalBridge,
)
from ..adapters.security.control_plane_paths import (
    ControlPlanePaths,
    control_plane_scope,
    live_control_plane_inventory,
)
from ..application.context import OperationContext
from ..application.ports.app_control import NotFound, identifier
from ..application.ports.app_control_http import ControlError
from ..application.ports.app_managed_work import AppWorkRecord
from ..application.ports.lane_continuation import VerificationApprovalPending
from ..platform import paths
from .app_control_http import _principal
from .app_managed_authority import AppManagedAuthority
from .app_managed_work import AppManagedWorkDispatcher, dispatch_approval_arguments
from .managed_conversation import ManagedConversationLifetime
from .managed_standalone import ManagedStandaloneSession
from .prepared_workbench import PreparedWorkbenchAdapter


def install_owned_work_http(control, *, application, runtime, permission_engine):
    """Called only by owned startup, after installing its slot, before listener."""
    from .managed_app_work import register_owned_app_work, require_owned_app_work

    if getattr(control, "_work_binding", None) is not None:
        raise PermissionError("app work HTTP composition already installed")
    result = AppManagedWorkHttpBinding(
        control,
        application=application,
        runtime=runtime,
        permission_engine=permission_engine,
        register_owned=register_owned_app_work,
        require_owned=require_owned_app_work,
    )
    control._work_binding = result
    return result


def current_work_http(control):
    result = getattr(control, "_work_binding", None)
    if result is None:
        return None
    if type(result) is not AppManagedWorkHttpBinding or result.control is not control:
        raise PermissionError("exact installed app work HTTP service required")
    result.require_current()
    return result


def public_work(record):
    if type(record) is not AppWorkRecord:
        raise TypeError("exact retained app work required")
    record.__post_init__()
    work = record.prepared
    result = dict(
        work_id=work.work_id,
        state=record.state,
        revision=record.revision,
        project=work.binding.grant.project_handle,
        expires_at=work.expires_at,
        options=dict(
            tier=work.plan.spec.tier,
            max_steps=work.plan.spec.max_steps,
            allow_web=work.plan.spec.allow_web,
            allow_location=work.plan.allow_location,
        ),
    )
    if record.verification_pending is not None:
        pending = record.verification_pending
        result["pending"] = dict(
            kind="verification_approval",
            identity=asdict(pending.identity),
            approval=asdict(pending.approval),
        )
    if record.interruption is not None:
        result["interruption"] = asdict(record.interruption)
    if record.completion is not None:
        # Phase is not an assertion that the original work succeeded.
        result["completion"] = dict(phase=record.completion.phase)
    return result


class _Cancellation:
    def __init__(self):
        self.event = threading.Event()

    @property
    def cancelled(self):
        return self.event.is_set()

    def wait(self, timeout=None):
        return self.event.wait(timeout)


class _AppWorkbench(PreparedWorkbenchAdapter):
    def __init__(self, service):
        self.service = service
        # This slot carries immutable policy DATA only, never a connection or
        # authority. Every execution still requires its actual managed factory.
        self._policy_data = ContextVar("app_work_policy_data", default=None)
        super().__init__(service.runtime, policy_snapshot=self._policy_data.get)

    def _resolve(self, spec, context, allow_location):
        token = self._policy_data.set(self.service.policy(context))
        try:
            return super()._resolve(spec, context, allow_location)
        finally:
            self._policy_data.reset(token)

    def execute_prepared_workbench(
        self, prepared, *, admitted_context, managed_factory
    ):
        self.service.inventory()
        with control_plane_scope(self.service.private_paths()), managed_root_scope(
            lambda: admitted_context.workspace_roots
        ):
            return super().execute_prepared_workbench(
                prepared,
                admitted_context=admitted_context,
                managed_factory=managed_factory,
            )


class AppManagedWorkHttpBinding:
    """Trusted startup supplies the exact owned registration and live getter."""

    def __init__(
        self,
        control,
        *,
        application,
        runtime,
        permission_engine,
        register_owned,
        require_owned,
        output_root=None
    ):
        if (
            control is None
            or application is None
            or runtime is None
            or permission_engine is None
            or not callable(register_owned)
            or not callable(require_owned)
        ):
            raise TypeError("explicit owned app work composition required")
        self.control, self.application, self.runtime = control, application, runtime
        self._require_owned = require_owned
        self.output_root = Path(
            output_root or paths.default_home() / "terminal-output"
        ).resolve()
        self.cancellation = _Cancellation()
        self._account_lock = threading.Lock()
        self._account_active = {}
        self.inventory()  # Private exclusion must precede any output-store creation.
        self.lanes = application.agent_lanes()
        self.authority = AppManagedAuthority(control, self.lanes)
        self.permission_engine = permission_engine
        self.ledger = permission_engine.approval_ledger().pinned()
        self.dispatcher = AppManagedWorkDispatcher(
            self.authority,
            _AppWorkbench(self),
            application=application,
            lifetime_factory=self._lifetime,
            authorize_dispatch=self._approve_work,
            terminal_eligibility=self._eligibility,
        )
        registration = None
        try:
            registration = register_owned(application, self.dispatcher)
            self.require_current()
            registration.commit()
        except BaseException:
            if registration is not None:
                registration.rollback(timeout=2)
            else:
                self.dispatcher.close()
            raise

    def require_current(self):
        if self._require_owned(self.application) is not self.dispatcher:
            raise PermissionError("exact owned app dispatcher unavailable")
        self.control._config()
        self.inventory()

    def model_roots(self):
        config = self.control._config_provider()
        roots = tuple(Path(value).resolve() for value in config.state.workspace_roots)
        if not 1 <= len(roots) <= 256 or any(not root.is_dir() for root in roots):
            raise PermissionError("complete live model workspace roots unavailable")
        return roots

    def private_paths(self):
        config = self.control._config_provider()
        return ControlPlanePaths(
            databases=(
                Path(self.control._fleet_path()),
                Path(self.control._account_path()),
            ),
            files=tuple(
                Path(p)
                for p in (
                    *config.private_source_paths,
                    *self.application.private_source_paths,
                    config.app_control.catalog_file,
                )
            ),
            owned_directories=(self.output_root,),
            owner_lock_directories=(Path(self.control._fleet_path()).parent,),
        )

    def inventory(self):
        result = live_control_plane_inventory(additional=self.private_paths)
        result.require_disjoint(self.model_roots())
        self.control._private()
        return result

    def policy(self, context):
        self.require_current()
        config = self.control._config_provider()
        # Only select host catalog policy data. A matching catalog entry never
        # replaces fresh account/control/selection admission on model/tool effects.
        matches = [
            grant
            for grant in self.control.catalog.snapshot().grants
            if grant.role == context.auth_level
            and any(
                context.principal_id
                == "account:" + hashlib.sha256(name.encode()).hexdigest()
                for name in grant.accounts
            )
            and tuple(Path(p) for p in grant.roots) == context.workspace_roots
        ]
        if len(matches) != 1:
            raise PermissionError("one unambiguous exact host project policy required")
        grant = matches[0]
        return dict(
            allowed_tools=sorted(set(grant.tools) & self.lanes.allowed_tools),
            allow_web=config.features.web,
            allow_location=config.features.location_consent,
            grant_digest=grant.digest,
            catalog_digest=grant.catalog_digest,
            project=grant.project,
        )

    def _bridge(self, guard):
        def decide(tool, **kwargs):
            guard()
            return self.permission_engine.decide(tool, interactive=False, **kwargs)

        return ContinuationApprovalBridge(
            ledger=self.ledger,
            decide=decide,
            digest_call=self.permission_engine.call_digest,
        )

    def _approve_work(self, work, context):
        self.require_current()
        return self._bridge(self.require_current).authorize(
            "workspace_run",
            dispatch_approval_arguments(work),
            surface="app-control",
            expires_at=work.expires_at,
        )

    def _lifetime(self, selection):
        def guard():
            self.require_current()
            self.authority.work_atomic(selection, selection.context, lambda tx: None)

        def context():
            guard()
            return selection.context

        def create(controller, application):
            if application is not self.application:
                raise PermissionError("owned Application changed")
            guard()
            output = SQLiteTerminalOutputStore(
                self.output_root, model_writable_roots=self.model_roots
            )
            codec = TerminalProjectionCodec(output_store=output, output_context=context)
            host = self.authority.continuation_service(
                selection, projection_codec=codec
            )
            return ManagedStandaloneSession(
                controller=controller,
                application=application,
                host=host,
                context=selection.context,
                host_conversation_id=selection.host_conversation_id,
                private_paths=lambda: self.inventory().admission_directories,
                model_writable_roots=self.model_roots,
                approve=lambda prepared, ctx: self._bridge(guard).authorize(
                    "workspace_run",
                    prepared.approval_payload(),
                    surface="app-control",
                    expires_at=min(
                        selection.control.expires_at,
                        time.time() + ctx.remaining_seconds,
                    ),
                ),
            )

        return ManagedConversationLifetime(
            application=self.application, session_factory=create, require_current=guard
        )

    def _eligibility(self, lifetime, expected, finalized):
        self.require_current()
        result = lifetime.terminal_eligibility(
            expected, verifier_factory=self.runtime._standalone_verifier_factory
        )
        if result.evidence.result != finalized:
            raise PermissionError("exact finalized app output required")
        return result

    def _issue(self, account_token, control_token, action, payload):
        self.require_current()
        conn = self.control._open()
        try:
            with account_admission(conn):
                account = self.control._account(conn, account_token)
                session, grant = self.control._session(account, control_token)
                expiry = min(account.expires_at, session.expires_at, grant.expires_at)
                if action == "execute_work":
                    row = self.control.store.atomic(
                        lambda tx: tx.read_work(
                            principal_id=_principal(account),
                            control_session_id=session.control_session_id,
                            work_id=payload["work_id"],
                        )
                    )
                    if row is None:
                        raise NotFound("work unavailable")
                    if row.state == "prepared":
                        expiry = min(expiry, row.prepared.expires_at)
                context = OperationContext(
                    "app-work-" + uuid.uuid4().hex,
                    _principal(account),
                    account.role,
                    "http",
                    time.monotonic() + max(0, expiry - time.time() - 0.01),
                    self.cancellation,
                    tuple(Path(p) for p in grant.roots),
                    False,
                    False,
                )
                self.control._current(conn, account_token, account, grant)
        finally:
            conn.close()
        return self.authority.issue_selection(
            account_token=account_token, control_token=control_token, context=context
        )

    def _publish(self, selection, account_token, control_token, result, publish):
        # No lifetime, future, or dispatcher call under account/fleet locks.
        # The worker may already have released this selection object.
        conn = self.control._open()
        try:
            with account_admission(conn):
                self.require_current()
                account = self.control._account(conn, account_token, selection.account)
                session, grant = self.control._session(account, control_token)
                if session != selection.control:
                    raise PermissionError("original control session changed")
                current = self.control.store.atomic(
                    lambda tx: tx.require_selection(
                        principal_id=session.principal_id,
                        control_session_id=session.control_session_id,
                        binding_id=selection.binding.binding_id,
                        binding_revision=selection.binding.revision,
                        selection_id=selection.slot.selection_id,
                        epoch=selection.slot.epoch,
                    )
                )
                if current != (session, selection.binding, selection.slot):
                    raise PermissionError("selected binding changed before publication")
                self.control._current(conn, account_token, account, grant)
                publish(*result)
        finally:
            conn.close()

    def perform(self, action, payload, *, account_token, control_token, publish):
        selection = None
        result = None
        publication_started = False
        account_held = False
        selection_released = False

        def once(*args):
            nonlocal publication_started
            if publication_started:
                raise PermissionError("response publication already attempted")
            publication_started = True
            return publish(*args)

        try:
            if type(payload) is not dict:
                raise ValueError("exact app work request required")
            if action == "prepare_work":
                allowed = {
                    "command_id",
                    "prompt",
                    "tier",
                    "max_steps",
                    "allow_web",
                    "allow_location",
                }
                if (
                    not {"command_id", "prompt"} <= set(payload)
                    or set(payload) - allowed
                ):
                    raise ValueError("exact preparation fields required")
                identifier(payload["command_id"])
            elif action in {"execute_work", "read_work"} and set(payload) == {
                "work_id"
            }:
                identifier(payload["work_id"])
            else:
                raise ValueError("unsupported app work operation")
            selection = self._issue(account_token, control_token, action, payload)
            with self._account_lock:
                principal = selection.control.principal_id
                if (
                    self._account_active.get(principal, 0) >= 2
                    or len(self._account_active) >= 64
                ):
                    raise ControlError(429, "APP_WORK_BUSY")
                self._account_active[principal] = (
                    self._account_active.get(principal, 0) + 1
                )
                account_held = True
            if action == "prepare_work":
                request = {k: v for k, v in payload.items() if k != "command_id"}
                request.setdefault("allow_web", False)
                row = self.dispatcher.prepare(
                    selection, command_id=payload["command_id"], request=request
                )
                receipt = self.authority.work_atomic(
                    selection,
                    selection.context,
                    lambda tx: tx.command(
                        row.prepared.command,
                        action="prepare_work",
                        argument_digest=row.prepared.digest,
                    ),
                )
                if receipt is None:
                    raise PermissionError("preparation receipt unavailable")
                result = (
                    200,
                    dict(
                        ok=True,
                        work=public_work(row),
                        receipt=asdict(receipt.public_receipt),
                    ),
                )
            elif action == "execute_work":
                row = self.dispatcher.execute(selection, work_id=payload["work_id"])
                result = (202, dict(ok=True, work=public_work(row)))
            else:
                row = self.dispatcher.status(selection, work_id=payload["work_id"])
                result = (200, dict(ok=True, work=public_work(row)))
        except VerificationApprovalPending as error:
            result = (
                409,
                dict(
                    ok=False,
                    error=dict(code="APP_WORK_APPROVAL_PENDING"),
                    pending=asdict(error.evidence),
                ),
            )
        except ApprovalOutcomeUnknown:
            result = (503, dict(ok=False, error=dict(code="APP_WORK_APPROVAL_UNKNOWN")))
        except Exception as error:
            result = self.control._error(error)
        if selection is not None:
            try:
                # Publication uses independently authenticated immutable scope,
                # not this issuer. Avoid holding idle request selections on I/O.
                self.authority.release_selection(selection)
                selection_released = True
            except PermissionError:
                pass  # Worker-owned or already released by completed worker.
        try:
            if selection is None:
                once(*result)
            else:
                try:
                    self._publish(selection, account_token, control_token, result, once)
                except Exception as error:
                    if publication_started:
                        raise
                    once(*self.control._error(error))
        finally:
            if account_held:
                with self._account_lock:
                    self._account_active[principal] -= 1
                    if not self._account_active[principal]:
                        del self._account_active[principal]
            if selection is not None and not selection_released:
                try:
                    self.authority.release_selection(selection)
                except PermissionError:
                    # Exact worker retention or already-completed worker release.
                    # Neither case permits the request to discard a worker lease.
                    pass
