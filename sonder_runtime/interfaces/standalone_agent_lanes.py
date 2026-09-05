"""Private first-level lane authority for one trusted standalone controller run."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import json
import logging
from pathlib import Path
import uuid
from typing import Protocol

from ..application.context import OperationContext, local_owner_context
from .agent_lane_entrypoint import lane_service
from .agent_lanes import dispatch_agent_lane_tool

_LOG = logging.getLogger(__name__)
_CURRENT = ContextVar("standalone_lane_controller", default=None)
_DEPTH = ContextVar("standalone_lane_controller_depth", default=0)
_LOOP_DEPTH = ContextVar("standalone_lane_loop_depth", default=0)
_MANAGED_FACTORY = ContextVar("private_managed_controller_factory", default=None)
_ACTIONS = frozenset(
    {
        "spawn",
        "list",
        "inspect",
        "send_message",
        "wait",
        "interrupt",
        "resume",
        "cancel",
        "reports",
        "ack",
    }
)
_ALIASES = {"message": "send_message", "report": "reports"}
_FORBIDDEN = frozenset(
    {
        "principal_id",
        "author",
        "context",
        "parent_session_id",
        "parent_lane_id",
        "parent_token",
        "token",
        "approval",
        "workspace_roots",
        "allowed_tools",
        "cloud_allowed",
        "remote_ollama_allowed",
        "grant_expires",
        "grant_id",
        "grant_revision",
    }
)


def current():
    return _CURRENT.get()


@dataclass(frozen=True)
class PreparedLaneCommand:
    """Host-owned immutable snapshot; callers receive detached approval arguments."""

    owner: object
    encoded: str

    def approval_arguments(self):
        return json.loads(self.encoded)


@dataclass(frozen=True)
class HostTerminalDraft:
    """Actual turn evidence awaiting scoped durable continuation linkage."""

    ledger_bytes: bytes
    output: str
    terminal_class: str
    blockers: tuple[str, ...]


class ManagedControllerSession(Protocol):
    context: OperationContext

    def require_current(self): ...
    def dispatch(self, prepared: PreparedLaneCommand): ...
    def report_metadata(self): ...
    def request_cancel(self): ...
    def close(self): ...
    def verify_delegated(self, draft: HostTerminalDraft, *, verifier_factory): ...


@contextmanager
def managed_controller_factory_scope(factory):
    """Trusted bootstrap injection only; never a model-visible argument."""
    if not callable(factory):
        raise TypeError("managed controller factory must be callable")
    token = _MANAGED_FACTORY.set(factory)
    try:
        yield
    finally:
        _MANAGED_FACTORY.reset(token)


class StandaloneLaneController:
    def __init__(self, application_factory, project=""):
        self._factory = application_factory
        self._project = project
        self.run_id = "standalone-" + uuid.uuid4().hex
        self._application = self._context = self._parent = None
        self._closed = self._cancelled = self._restricted = False
        self.delegated_work = False
        self.terminal_projected = False
        self._verification_attempted = False
        self._verifier = self._verification_prepared = None
        self._verification_verdict = None
        self._host_ledger = self._host_terminal = None
        self._host_evidence_error = False
        self._managed_factory = (
            _MANAGED_FACTORY.get() if not (_DEPTH.get() or _LOOP_DEPTH.get()) else None
        )
        self._managed_session = None
        self._managed_initialization_failed = False

    def require_current(self):
        """Root host guard; managed authority is checked at each admission."""
        self._initialize()
        if self._context.expired or self._context.cancellation.cancelled:
            raise PermissionError(
                "standalone controller authority expired or cancelled"
            )

    def _guard_host_evidence(self):
        if self._managed_factory is not None:
            self.require_current()

    def begin_host_turn(self, ledger):
        self._guard_host_evidence()
        # The composition root owns the adapter; interfaces only consume its
        # host-only observe/seal seam.
        self._host_ledger = ledger
        self._host_terminal = None
        self._host_evidence_error = False

    def observe_host_tool(self, **facts):
        self._guard_host_evidence()
        if self._host_ledger is None or self._host_terminal is not None:
            self._host_evidence_error = True
            return
        try:
            self._host_ledger.observe(**facts)
        except ValueError:
            # Retain an unavailable state. No delegated verification may use
            # an incomplete ledger even if later calls themselves succeed.
            self._host_evidence_error = True

    def freeze_host_terminal(self, output, *, terminal_class, blockers):
        self._guard_host_evidence()
        if self._host_evidence_error or self._host_ledger is None:
            return False
        try:
            ledger_bytes = self._host_ledger.seal()
        except ValueError:
            self._host_evidence_error = True
            return False
        candidate = HostTerminalDraft(
            ledger_bytes, output, terminal_class, tuple(blockers)
        )
        if self._host_terminal is not None and self._host_terminal != candidate:
            self._host_evidence_error = True
            return False
        self._host_terminal = candidate
        return True

    def host_terminal_draft(self):
        self._guard_host_evidence()
        if self._host_evidence_error or self._host_terminal is None:
            raise PermissionError(
                "complete original host terminal evidence unavailable"
            )
        return self._host_terminal

    def verify_delegated(self, approve, *, verifier_factory):
        """Host-only finalization: one admission, then fresh typed validation.

        The original controller authority and exact catalog bundle are retained
        privately. A repeated terminal projection cannot launch another check.
        """
        from ..application.ports.delegated_verification import VerificationVerdict

        refused = VerificationVerdict(False, "VERIFICATION_UNAVAILABLE")
        self._verification_verdict = refused
        try:
            self._initialize()
            if self._managed_session is not None:
                if not self.delegated_work:
                    return refused
                self.require_current()
                verdict = self._managed_session.verify_delegated(
                    self.host_terminal_draft(),
                    verifier_factory=verifier_factory,
                )
                self.require_current()
                if (
                    not isinstance(verdict, VerificationVerdict)
                    or type(verdict.valid) is not bool
                ):
                    return refused
                self._verification_verdict = verdict
                return verdict
            if self._parent is None or not self.delegated_work:
                return refused
            service = lane_service(self._application)
            service.verify_model_parent(
                self._parent["parent_session_id"],
                self._parent["parent_token"],
                self._context,
            )
            parent = self._parent["parent_session_id"]
            revision = self._parent["revision"]
            if not self._verification_attempted:
                # Set before composition/admission: even an ambiguous failure
                # may have durable effects and must not cause blind replay.
                self._verification_attempted = True
                self._verifier = verifier_factory(self._application, service)
                self._verification_prepared = self._verifier.prepare(
                    parent,
                    command_id=self.run_id + "-verify",
                    context=self._context,
                    bound_parent_revision=revision,
                )
                self._verifier.execute_prepared(
                    self._verification_prepared,
                    context=self._context,
                    approve=approve,
                )
            prepared = self._verification_prepared
            if prepared is None:
                return refused
            verdict = self._verifier.validate(
                parent,
                prepared.verification_id,
                context=self._context,
                bound_parent_revision=revision,
            )
            if (
                not isinstance(verdict, VerificationVerdict)
                or type(verdict.valid) is not bool
            ):
                return refused
            if verdict.valid is True and not (
                verdict.code == "CERTIFIED"
                and verdict.certificate_id == prepared.verification_id
                and verdict.parent_session_id == parent == prepared.parent_session_id
                and verdict.parent_grant_revision
                == revision
                == prepared.parent_grant_revision
                and verdict.generation == prepared.generation
                and verdict.roots == prepared.roots
                and verdict.children == prepared.children
            ):
                return refused
            self._verification_verdict = verdict
            return verdict
        except Exception:
            _LOG.warning("standalone delegated verification unavailable")
            return refused

    def restrict(self, *, read_only=False, cloud=False):
        self._restricted = self._restricted or read_only or cloud

    @property
    def available(self):
        return not (self._closed or self._cancelled or self._restricted)

    def _initialize(self):
        if not self.available:
            raise PermissionError("standalone lane authority is not active")
        if self._managed_initialization_failed:
            raise PermissionError("managed controller initialization unavailable")
        if self._application is not None:
            if self._managed_session is not None:
                self._managed_session.require_current()
            return
        if self._managed_factory is not None:
            # Claim before invoking host composition: uncertain initialization
            # must never retry or silently mint legacy bearer authority.
            self._managed_initialization_failed = True
            session = None
            try:
                application = self._factory()
                session = self._managed_factory(self, application)
                if not isinstance(session.context, OperationContext):
                    raise TypeError(
                        "managed session requires admitted operation context"
                    )
                session.require_current()
                if session.context.expired or session.context.cancellation.cancelled:
                    raise PermissionError("managed session context is not live")
                self._context = session.context
                self._managed_session = session
                self._application = application
                self._managed_initialization_failed = False
                return
            except BaseException:
                if session is not None:
                    try:
                        session.close()
                    except Exception:
                        _LOG.warning(
                            "managed controller initialization cleanup unavailable"
                        )
                raise
        application = self._factory()
        roots = tuple(
            Path(root).resolve() for root in application.config.state.workspace_roots
        )
        if self._project:
            project = Path(self._project).resolve()
            if not project.is_dir():
                raise PermissionError("standalone project grant is unavailable")
            roots = tuple(
                project if project.is_relative_to(root) else root
                for root in roots
                if project.is_relative_to(root) or root.is_relative_to(project)
            )
        roots = tuple(dict.fromkeys(root for root in roots if root.is_dir()))
        if not roots:
            raise PermissionError(
                "standalone lane control requires a configured workspace grant"
            )
        self._context = local_owner_context(
            correlation_id=self.run_id,
            source="repl",
            workspace_roots=roots,
            timeout_seconds=3600,
            remote_ollama_allowed=application.config.ollama.allow_remote,
        )
        self._application = application

    def prepare(self, arguments):
        if not isinstance(arguments, dict) or set(arguments) != {"action", "payload"}:
            raise PermissionError(
                "standalone lane arguments accept only action and payload"
            )
        if not isinstance(arguments["action"], str):
            raise ValueError("lane action must be a string")
        action = _ALIASES.get(arguments["action"], arguments["action"])
        if action not in _ACTIONS:
            raise ValueError("unknown standalone lane action")
        payload = arguments["payload"]
        if not isinstance(payload, dict) or len(payload) > 32:
            raise ValueError("lane payload must be a bounded object")
        if _FORBIDDEN.intersection(payload):
            raise PermissionError("lane parent identity and grants are host-owned")
        self.require_current()
        if self._context.expired:
            raise PermissionError("standalone lane controller grant expired")
        payload = dict(payload)
        if action == "spawn":
            raw = payload.get("workspace_root")
            if not isinstance(raw, str) or not raw:
                raise ValueError("spawn requires workspace_root")
            root = Path(raw)
            if not root.is_absolute():
                if len(self._context.workspace_roots) != 1:
                    raise PermissionError(
                        "relative child workspace requires one unambiguous granted root"
                    )
                root = self._context.workspace_roots[0] / root
            root = root.resolve()
            if not any(
                root.is_relative_to(granted)
                for granted in self._context.workspace_roots
            ):
                raise PermissionError("lane workspace escapes the standalone run grant")
            payload["workspace_root"] = str(root)
        return {
            "action": action,
            "payload": payload,
            "standalone_run_id": self.run_id,
            "workspace_roots": [str(root) for root in self._context.workspace_roots],
            "principal_id": self._context.principal_id,
        }

    def prepare_command(self, arguments):
        return PreparedLaneCommand(
            self, json.dumps(self.prepare(arguments), allow_nan=False)
        )

    def execute(self, arguments):
        # Trusted convenience path; model dispatch must approve a prepared snapshot.
        return self.execute_prepared(self.prepare_command(arguments))

    def execute_prepared(self, prepared):
        if not isinstance(prepared, PreparedLaneCommand) or prepared.owner is not self:
            raise PermissionError("prepared lane command belongs to another controller")
        self._initialize()  # live cancellation/restriction check, no argument rewriting
        if self._context.expired:
            raise PermissionError("standalone lane controller grant expired")
        safe = prepared.approval_arguments()
        if safe["action"] == "spawn":
            root = Path(safe["payload"]["workspace_root"])
            if root.resolve() != root:
                raise PermissionError("approved lane workspace resolution changed")
        if self._managed_session is not None:
            self.require_current()
            if safe["action"] in {"spawn", "send_message", "resume"}:
                self.delegated_work = True
            return self._managed_session.dispatch(prepared)
        service = lane_service(self._application)
        if self._parent is None:
            self._parent = service.open_model_parent(self._context)
        parent = self._parent["parent_session_id"]
        service.verify_model_parent(parent, self._parent["parent_token"], self._context)
        if safe["action"] in {"spawn", "send_message", "resume"}:
            # An ambiguous failing admission may still have durable effects.
            self.delegated_work = True
        return dispatch_agent_lane_tool(
            service,
            safe["action"],
            safe["payload"],
            self._context,
            parent_session_id=parent,
            bound_parent_session_id=parent,
        )

    def report_outcome(self, output):
        text = str(output or "")
        # Preserve failure markers consumed by existing callers.
        first = text.lstrip().splitlines()[0] if text.strip() else ""
        failure = (
            first
            if first.startswith(
                ("ERROR", "EVIDENCE_REQUIRED", "VALIDATION_FAILED", "CANCELLED")
            )
            else ""
        )
        return ((failure + "\n") if failure else "") + (
            "UNVERIFIED: delegated work requires verification after child activity is quiescent. "
            "This run does not certify the child's workspace changes.\n\n"
            "Unverified model outcome:\n"
            + text
            + "\n\n=== DELEGATED WORK METADATA ===\n"
            + json.dumps(self.report_metadata(), sort_keys=True)
        )

    def report_metadata(self):
        metadata = {
            "standalone_run_id": self.run_id,
            "verification": "delegated-work-verification-required",
            "children": [],
        }
        if self._managed_factory is not None:
            try:
                self.require_current()
                return self._managed_session.report_metadata()
            except Exception:
                metadata["child_state"] = "unavailable"
                return metadata
        if self._parent is None:
            return metadata
        metadata["parent_session_id"] = self._parent["parent_session_id"]
        try:
            children = lane_service(self._application).list(
                self._context,
                parent_session_id=self._parent["parent_session_id"],
                limit=100,
            )["lanes"]
            metadata["children"] = [
                {key: lane[key] for key in ("id", "revision", "status")}
                for lane in children
            ]
        except Exception:
            metadata["child_state"] = "unavailable"
        return metadata

    def request_cancel(self):
        if self._closed or self._cancelled:
            return
        self._cancelled = True
        if self._managed_session is not None:
            try:
                self._managed_session.request_cancel()
            except Exception:
                _LOG.warning("managed controller cancellation unavailable")
            return
        if self._parent is None:
            return
        service = lane_service(self._application)
        try:
            service.verify_model_parent(
                self._parent["parent_session_id"],
                self._parent["parent_token"],
                self._context,
            )
            children = service.list(
                self._context,
                parent_session_id=self._parent["parent_session_id"],
                limit=100,
            )
            for lane in children["lanes"]:
                try:
                    service.control(
                        lane["id"],
                        "cancel",
                        command_id=self.run_id + "-cancel-" + lane["id"],
                        context=self._context,
                        reason="standalone controller cancelled",
                        author="parent",
                    )
                except Exception:
                    _LOG.warning("standalone child cancellation could not be recorded")
        except Exception:
            _LOG.warning("standalone child cancellation could not be fully recorded")

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._managed_session is not None:
            self._managed_session.close()
        self._parent = None  # independent children retain their existing bounded grants


@contextmanager
def controller_scope(application_factory, *, project=""):
    # A recursively invoked agent must not inherit or mint another controller.
    controller = (
        None if _DEPTH.get() else StandaloneLaneController(application_factory, project)
    )
    depth_token = _DEPTH.set(_DEPTH.get() + 1)
    token = _CURRENT.set(controller)
    try:
        yield controller
    except BaseException:
        if controller is not None:
            controller.request_cancel()
        raise
    finally:
        try:
            if controller is not None:
                controller.close()
        finally:
            _CURRENT.reset(token)
            _DEPTH.reset(depth_token)


@contextmanager
def model_loop_scope():
    """Only the root loop may use the controller; tool-invoked loops cannot."""
    depth = _LOOP_DEPTH.get()
    depth_token = _LOOP_DEPTH.set(depth + 1)
    token = _CURRENT.set(None) if depth else None
    try:
        yield
    finally:
        if token is not None:
            _CURRENT.reset(token)
        _LOOP_DEPTH.reset(depth_token)
