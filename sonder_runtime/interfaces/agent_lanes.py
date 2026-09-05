"""Shared JSON command adapter for trusted HTTP and model surfaces."""

from __future__ import annotations


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
    if bound_parent_session_id is None or bound_parent_session_id != parent_session_id:
        raise PermissionError(
            "model parent scope must be bound by a verified invocation"
        )
    if not isinstance(payload, dict):
        raise ValueError("lane payload must be an object")
    if any(
        key in payload
        for key in (
            "principal_id",
            "author",
            "parent_session_id",
            "parent_lane_id",
            "context",
        )
    ):
        raise PermissionError(
            "lane identity and author come from the trusted invocation"
        )
    args = dict(payload)
    if parent_lane_id:
        parent = service.inspect(parent_lane_id, context)["lane"]
        if parent["session_id"] != parent_session_id:
            raise PermissionError("parent lane canonical session mismatch")
    lane_id = args.pop("lane_id", None)
    if lane_id and parent_lane_id:
        target = service.inspect(lane_id, context)["lane"]
        if target["parent_lane_id"] != parent_lane_id:
            raise PermissionError(
                "model may only control its directly delegated children"
            )
    elif lane_id:
        target = service.inspect(lane_id, context)["lane"]
        if target["parent_session_id"] != parent_session_id:
            raise PermissionError("model parent session does not match lane")
    if action == "spawn":
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
        report_id = args.pop("report_id")
        return service.ack_report(
            report_id, context=context, parent_session_id=parent_session_id, **args
        )
    if not lane_id:
        raise ValueError("lane_id required")
    if action == "inspect":
        return service.inspect(lane_id, context, **args)
    if action == "send_message":
        return service.send_message(lane_id, author="parent", context=context, **args)
    if action == "wait":
        return service.wait(lane_id, context, **args)
    if action in {"interrupt", "resume", "cancel"}:
        return service.control(
            lane_id, action, context=context, author="parent", **args
        )
    raise ValueError("unknown agent lane action")
