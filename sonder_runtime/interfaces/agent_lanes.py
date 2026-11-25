"""Shared JSON command adapter for trusted HTTP and model surfaces."""

from __future__ import annotations

_MAX_ID_CHARS = 160
_MAX_PAYLOAD_KEYS = 32
_RESERVED_KEYS = frozenset(
    {
        "principal_id",
        "author",
        "parent_session_id",
        "parent_lane_id",
        "context",
        "bound_parent_session_id",
    }
)
_ACTION_ALLOWED_KEYS: dict[str, frozenset[str]] = {
    "spawn": frozenset(
        {
            "command_id",
            "task",
            "workspace_root",
            "lane_id",
            "title",
            "tier",
            "max_steps",
            "max_output_tokens",
            "max_wall_seconds",
        }
    ),
    "list": frozenset({"cursor", "limit", "newest_first"}),
    "reports": frozenset({"cursor", "limit"}),
    "ack": frozenset({"report_id", "command_id"}),
    "inspect": frozenset({"lane_id", "cursor", "limit"}),
    "retrieve_archive": frozenset({"lane_id", "archive_id"}),
    "send_message": frozenset({"lane_id", "command_id", "content"}),
    "wait": frozenset({"lane_id", "cursor", "limit", "timeout_seconds"}),
    "interrupt": frozenset({"lane_id", "command_id", "reason", "content"}),
    "resume": frozenset({"lane_id", "command_id", "reason", "content"}),
    "cancel": frozenset({"lane_id", "command_id", "reason", "content"}),
}


def _bounded_identifier(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > _MAX_ID_CHARS
        or "\x00" in value
    ):
        raise ValueError(f"{name} must be nonempty bounded text")
    return value.strip()


def dispatch_agent_lane_tool(
    service,
    action,
    payload,
    context,
    parent_session_id,
    parent_lane_id=None,
    *,
    bound_parent_session_id=None,
):
    if (
        not isinstance(bound_parent_session_id, str)
        or not bound_parent_session_id.strip()
        or not isinstance(parent_session_id, str)
        or bound_parent_session_id != parent_session_id
    ):
        raise PermissionError(
            "model parent scope must be bound by a verified invocation"
        )
    if not isinstance(payload, dict) or len(payload) > _MAX_PAYLOAD_KEYS:
        raise ValueError("lane payload must be a bounded object")
    if any(not isinstance(key, str) for key in payload):
        raise ValueError("lane payload keys must be strings")
    if any(key in payload for key in _RESERVED_KEYS):
        raise PermissionError(
            "lane identity and author come from the trusted invocation"
        )
    if not isinstance(action, str) or action not in _ACTION_ALLOWED_KEYS:
        raise ValueError("unknown agent lane action")
    unexpected = set(payload) - _ACTION_ALLOWED_KEYS[action]
    if unexpected:
        raise ValueError(
            "unexpected argument(s) for agent lane action %s: %s"
            % (action, ", ".join(sorted(unexpected)[:4]))
        )
    args = dict(payload)
    if parent_lane_id is not None:
        parent_lane_id = _bounded_identifier(parent_lane_id, "parent_lane_id")
        parent = service.inspect(parent_lane_id, context)["lane"]
        if parent["session_id"] != parent_session_id:
            raise PermissionError("parent lane canonical session mismatch")
    if action == "spawn":
        if "lane_id" in args and args["lane_id"] is not None:
            args["lane_id"] = _bounded_identifier(args["lane_id"], "lane_id")
        return service.spawn(
            parent_session_id=parent_session_id,
            parent_lane_id=parent_lane_id,
            context=context,
            **args,
        )
    if action == "list":
        return service.list(context, parent_session_id=parent_session_id, **args)
    if action == "reports":
        return service.reports(parent_session_id, context, **args)
    if action == "ack":
        raw_report_id = args.pop("report_id", None)
        if not isinstance(raw_report_id, str) or not raw_report_id.strip():
            raise ValueError("report_id required")
        report_id = _bounded_identifier(raw_report_id, "report_id")
        return service.ack_report(
            report_id, context=context, parent_session_id=parent_session_id, **args
        )
    raw_lane_id = args.pop("lane_id", None)
    if raw_lane_id is None or (isinstance(raw_lane_id, str) and not raw_lane_id.strip()):
        raise ValueError("lane_id required")
    lane_id = _bounded_identifier(raw_lane_id, "lane_id")
    if parent_lane_id:
        target = service.inspect(lane_id, context)["lane"]
        if target["parent_lane_id"] != parent_lane_id:
            raise PermissionError(
                "model may only control its directly delegated children"
            )
    else:
        target = service.inspect(lane_id, context)["lane"]
        if target["parent_session_id"] != parent_session_id:
            raise PermissionError("model parent session does not match lane")
    if action == "inspect":
        return service.inspect(lane_id, context, **args)
    if action == "retrieve_archive":
        return service.retrieve_archived_tool(
            lane_id, args.pop("archive_id", None), context
        )
    if action == "send_message":
        return service.send_message(lane_id, author="parent", context=context, **args)
    if action == "wait":
        return service.wait(lane_id, context, **args)
    if action in {"interrupt", "resume", "cancel"}:
        return service.control(
            lane_id, action, context=context, author="parent", **args
        )
    raise ValueError("unknown agent lane action")
