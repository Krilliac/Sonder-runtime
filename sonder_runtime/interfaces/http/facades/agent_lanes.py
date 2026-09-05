"""Authenticated HTTP transport mapping for interactive agent lanes."""

from dataclasses import dataclass
from ....application.errors import CapacityExceeded


@dataclass(frozen=True)
class AgentLaneHttpResult:
    body: dict
    status_code: int = 200


def dispatch_agent_lane_route(service, method, path, payload, query, context):
    prefix = "/v1/agent-lanes"
    if path != prefix and not path.startswith(prefix + "/"):
        return None
    try:
        if not isinstance(payload, dict) or not isinstance(query, dict):
            raise ValueError("request and query must be objects")
        if any(
            key in payload
            for key in (
                "principal_id",
                "author",
                "context",
                "allowed_tools",
                "cloud_allowed",
                "remote_ollama_allowed",
            )
        ):
            raise PermissionError("authority is inherited from authenticated context")
        # Generated lane/report IDs contain only URI-safe characters. Reject
        # encoded path aliases instead of changing their scoped identity here.
        if "%" in path:
            raise ValueError("encoded agent route identifiers are not supported")
        parts = [p for p in path[len(prefix) :].strip("/").split("/") if p]

        def q(name, default=None):
            value = query.get(name, default)
            return value[0] if isinstance(value, list) else value

        bounds = dict(cursor=int(q("cursor", 0)), limit=int(q("limit", 50)))
        if not parts:
            if method == "GET":
                result = service.list(
                    context, parent_session_id=q("parent_session_id"), **bounds
                )
            elif method == "POST":
                result = service.spawn(context=context, author="user", **payload)
            else:
                return AgentLaneHttpResult({"error": "METHOD_NOT_ALLOWED"}, 405)
        elif parts[0] == "reports":
            if len(parts) == 1 and method == "GET":
                parent = q("parent_session_id")
                if not parent:
                    raise ValueError("parent_session_id required")
                result = service.reports(parent, context, **bounds)
            elif len(parts) == 3 and parts[2] == "ack" and method == "POST":
                result = service.ack_report(parts[1], context=context, **payload)
            else:
                return AgentLaneHttpResult({"error": "NOT_FOUND"}, 404)
        elif len(parts) == 1 and method == "GET":
            result = service.inspect(parts[0], context, **bounds)
        elif len(parts) == 2 and parts[1] == "wait" and method == "GET":
            result = service.wait(
                parts[0],
                context,
                timeout_seconds=float(q("timeout_seconds", 25)),
                **bounds
            )
        elif len(parts) == 2 and method == "POST":
            if parts[1] == "messages":
                result = service.send_message(
                    parts[0], context=context, author="user", **payload
                )
            elif parts[1] in {"interrupt", "resume", "cancel"}:
                result = service.control(parts[0], parts[1], context=context, **payload)
            else:
                return AgentLaneHttpResult({"error": "NOT_FOUND"}, 404)
        else:
            return AgentLaneHttpResult({"error": "NOT_FOUND"}, 404)
        return AgentLaneHttpResult(result, 202 if method == "POST" else 200)
    except CapacityExceeded:
        return AgentLaneHttpResult(
            {
                "error": "CAPACITY_EXCEEDED",
                "message": "agent lane wait capacity reached",
            },
            429,
        )
    except PermissionError as exc:
        return AgentLaneHttpResult({"error": "FORBIDDEN", "message": str(exc)}, 403)
    except KeyError:
        return AgentLaneHttpResult(
            {"error": "NOT_FOUND", "message": "lane or report not found"}, 404
        )
    except (TypeError, ValueError) as exc:
        return AgentLaneHttpResult({"error": "INVALID_INPUT", "message": str(exc)}, 400)
