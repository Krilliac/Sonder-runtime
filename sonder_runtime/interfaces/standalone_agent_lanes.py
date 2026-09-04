"""Private first-level lane authority for one trusted standalone controller run."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import logging
from pathlib import Path
import uuid

from ..application.context import local_owner_context
from .agent_lane_entrypoint import lane_service
from .agent_lanes import dispatch_agent_lane_tool

_LOG = logging.getLogger(__name__)
_CURRENT = ContextVar("standalone_lane_controller", default=None)
_DEPTH = ContextVar("standalone_lane_controller_depth", default=0)
_LOOP_DEPTH = ContextVar("standalone_lane_loop_depth", default=0)
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


class StandaloneLaneController:
    def __init__(self, application_factory, project=""):
        self._factory = application_factory
        self._project = project
        self.run_id = "standalone-" + uuid.uuid4().hex
        self._application = self._context = self._parent = None
        self._closed = self._cancelled = self._restricted = False
        self.delegated_work = False

    def restrict(self, *, read_only=False, cloud=False):
        self._restricted = self._restricted or read_only or cloud

    @property
    def available(self):
        return not (self._closed or self._cancelled or self._restricted)

    def _initialize(self):
        if not self.available:
            raise PermissionError("standalone lane authority is not active")
        if self._application is not None:
            return
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
        self._initialize()
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

    def execute(self, arguments):
        safe = self.prepare(arguments)
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

    def report_metadata(self):
        metadata = {
            "standalone_run_id": self.run_id,
            "verification": "delegated-work-verification-required",
            "children": [],
        }
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
        self._closed = True
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
        if controller is not None:
            controller.close()
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
