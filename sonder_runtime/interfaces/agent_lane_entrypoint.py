"""Parent capabilities and nonsecret approval identity at external boundaries."""
from __future__ import annotations

import hashlib
import re

from ..application.errors import DependencyUnavailable


ACTIONS = frozenset({
    "open_parent", "rotate_parent", "revoke_parent", "spawn", "list", "inspect",
    "send_message", "wait", "interrupt", "resume", "cancel", "reports", "ack",
})


def http_parent_scope(value, principal):
    """Resolve an HTTP alias within the caller's dedicated lane namespace.

    Already returned IDs roundtrip within that same principal. This namespace
    prevents an ordinary chat ID from becoming a destination for lane events;
    it is not a substitute for the service's authenticated ownership checks.
    """
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        raise ValueError("parent_session_id must be a bounded nonempty string")
    prefix = "lane-http-" + hashlib.sha256(principal.encode()).hexdigest()[:24] + "-"
    if value.startswith(prefix) and re.fullmatch(r"[0-9a-f]{64}", value[len(prefix):]):
        return value
    return prefix + hashlib.sha256(value.encode()).hexdigest()


def lane_service(application):
    factory = getattr(application, "agent_lanes", None)
    if not callable(factory):
        raise DependencyUnavailable("agent conversations are unavailable")
    return factory()


def lane_approval_arguments(application, context, arguments):
    """Verify bearer proof before approval lookup, excluding it from receipts.

    Root ID remains in the approval digest so an approval of one conversation
    cannot be used for another. Opening a parent always allocates a new root;
    callers cannot use it to attach themselves to an existing session.
    """
    if not isinstance(arguments, dict) or set(arguments) - {
        "action", "payload", "parent_session_id", "parent_token",
    }:
        raise ValueError("invalid agent command fields")
    action = arguments.get("action")
    payload = arguments.get("payload")
    if not isinstance(action, str) or action not in ACTIONS:
        raise ValueError("unknown agent action")
    if not isinstance(payload, dict) or len(payload) > 32:
        raise ValueError("payload must be a bounded object")
    parent = arguments.get("parent_session_id", "")
    proof = arguments.get("parent_token", "")
    if action == "open_parent":
        if parent or proof or payload:
            raise ValueError("open_parent requires an empty payload and no existing identity")
    else:
        if not isinstance(parent, str) or not 1 <= len(parent) <= 128:
            raise PermissionError("parent authority is invalid")
        if not isinstance(proof, str) or not 16 <= len(proof) <= 256:
            raise PermissionError("parent authority is invalid")
        lane_service(application).verify_model_parent(parent, proof, context)
    return {
        "action": action, "payload": dict(payload), "parent_session_id": parent,
        "principal_id": context.principal_id,
        "workspace_roots": [str(root) for root in context.workspace_roots],
    }


def execute_lane_command(application, context, arguments):
    """Execute only with a currently valid root capability or create a new root."""
    safe = lane_approval_arguments(application, context, arguments)
    service = lane_service(application)
    action = safe["action"]
    if action == "open_parent":
        return service.open_model_parent(context)
    parent = safe["parent_session_id"]
    if action in {"rotate_parent", "revoke_parent"}:
        operation = service.rotate_model_parent if action == "rotate_parent" else service.revoke_model_parent
        return operation(parent, arguments["parent_token"], context)
    from .agent_lanes import dispatch_agent_lane_tool
    return dispatch_agent_lane_tool(
        service, action, safe["payload"], context, parent_session_id=parent,
        bound_parent_session_id=parent,
    )
