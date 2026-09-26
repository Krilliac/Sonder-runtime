"""The one HTTP-to-typed-gateway call shared by the developer tool facades.

``/v1/build/*`` and ``/v1/tools/test-run`` / ``output-digest`` each parse
their own routes, then send exactly one typed tool call through the runtime's
typed gateway here: as the authenticated principal, with ``source="http"``, so
the permission modes grade it unattended (there is nobody at a console). The
schema, resource policy, permission modes, one-shot approvals, redaction and
the durable receipt apply exactly as on the native MCP and REPL surfaces.

This module only parses, maps and shapes responses; it holds no service.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from ....application.errors import Cancelled, DeadlineExceeded, Forbidden, InvalidInput
from ....application.tools.gateway_contract import (
    ToolGatewayRequest,
    ToolPermission,
    ToolScope,
)

MAX_BODY_KEYS = 32


class MethodNotAllowed(Exception):
    """The route exists but not for this HTTP method."""


class UnknownRoute(Exception):
    """The path is under a facade's prefix but names no route."""


def error_response(status: int, code: str, message: str = "", **extra) -> tuple[int, dict]:
    body: dict[str, Any] = {"error": {"code": code}}
    if message:
        body["error"]["message"] = str(message)[:400]
    body["error"].update(extra)
    return status, body


def parse_query(query: Mapping[str, list[str]], allowed: Mapping[str, type]) -> dict:
    """Typed single-valued query parameters; anything else is ``InvalidInput``."""
    if not isinstance(query, Mapping):
        raise InvalidInput("query must be a mapping")
    unknown = set(query) - set(allowed)
    if unknown:
        raise InvalidInput("unknown query parameter: %s" % sorted(unknown)[0][:40])
    out: dict[str, Any] = {}
    for key, values in query.items():
        if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], str):
            raise InvalidInput("each query parameter may appear once")
        raw = values[0]
        kind = allowed[key]
        if kind is int:
            if not re.fullmatch(r"\d{1,6}", raw):
                raise InvalidInput("%s must be a non-negative integer" % key)
            out[key] = int(raw)
        elif kind is bool:
            if raw not in ("true", "false", "1", "0"):
                raise InvalidInput("%s must be true or false" % key)
            out[key] = raw in ("true", "1")
        else:
            if len(raw) > 1024 or "\x00" in raw:
                raise InvalidInput("%s is too long" % key)
            out[key] = raw
    return out


def parse_body(payload: Any, allowed: frozenset[str]) -> dict:
    """A JSON object body restricted to ``allowed`` keys (absent body = ``{}``)."""
    if payload is None:
        return {}
    if not isinstance(payload, dict) or len(payload) > MAX_BODY_KEYS:
        raise InvalidInput("request body must be a JSON object")
    unknown = set(payload) - allowed
    if unknown:
        raise InvalidInput("unknown field: %s" % sorted(unknown)[0][:40])
    return dict(payload)


def parse_output(output: Any) -> dict:
    if isinstance(output, Mapping):
        return dict(output)
    try:
        value = json.loads(output) if isinstance(output, str) and output else {}
    except ValueError:
        return {"output": str(output)[:2000]}
    return value if isinstance(value, dict) else {"output": value}


@dataclass(frozen=True)
class GatewayErrorCodes:
    """The facade-specific error codes and status map for one route family."""

    request_prefix: str
    unavailable: str
    invalid: str
    abandoned: str
    too_large: str
    failed: str
    status_by_code: Mapping[str, int]
    remedies: tuple[str, ...]
    max_response_bytes: int


def execute_typed_call(tools_getter: Callable[[], Any], tool: str, arguments: Mapping[str, Any],
                       codes: GatewayErrorCodes, *, principal_id: str,
                       workspace_roots: tuple[str, ...] = (), auth_level: str = "user",
                       deadline_monotonic: float | None = None) -> tuple[int, dict]:
    """Run one typed tool call through the gateway; ``(status, body)``.

    A success body carries ``ok`` and the gateway receipt; the caller picks
    the success status (200, or 202 for a still-running job).
    """
    tools = tools_getter() if callable(tools_getter) else None
    if tools is None:
        return error_response(503, codes.unavailable, "the typed tool gateway is not composed")
    descriptor = tools.graph.registry.get(tool)
    if descriptor is None:
        return error_response(503, codes.unavailable, "the tool %s is not registered" % tool)
    effects = frozenset(effect.name.lower() for effect in descriptor.effects)
    try:
        request = ToolGatewayRequest(
            request_id=codes.request_prefix + uuid.uuid4().hex,
            tool_name=tool,
            arguments=dict(arguments),
            scope=ToolScope(principal_id=str(principal_id), workspace_roots=tuple(
                str(root) for root in workspace_roots), allowed_effects=effects,
                source="http", auth_level=auth_level),
            permission=ToolPermission(effects),
            deadline_monotonic=deadline_monotonic,
            execution_world="local",
        )
        receipt = tools.execute(request)
    except Forbidden as exc:
        decision = getattr(exc, "decision", None)
        decision = dict(decision) if isinstance(decision, Mapping) else {}
        if decision.get("stage") == "plan":
            code = str(decision.get("error_code") or codes.invalid)
            return error_response(codes.status_by_code.get(code, 400), code, str(exc))
        return error_response(403, "PERMISSION_DENIED", str(exc), decision=decision,
                              remedies=list(codes.remedies))
    except (Cancelled, DeadlineExceeded) as exc:
        return error_response(503, codes.abandoned, type(exc).__name__)
    except (InvalidInput, ValueError, TypeError) as exc:
        return error_response(400, codes.invalid, str(exc))
    body = parse_output(receipt.output)
    if not receipt.success:
        code = str(receipt.error_code or body.get("error_code") or codes.failed)
        return error_response(codes.status_by_code.get(code, 400), code,
                              str(body.get("message") or receipt.error or ""))
    body.setdefault("ok", True)
    body["receipt"] = {"request_id": receipt.request_id, "policy_match": receipt.policy_match}
    if len(json.dumps(body, ensure_ascii=True).encode("utf-8")) > codes.max_response_bytes:
        return error_response(413, codes.too_large, "narrow the request")
    return 200, body


__all__ = [
    "GatewayErrorCodes", "MAX_BODY_KEYS", "MethodNotAllowed", "UnknownRoute",
    "error_response", "execute_typed_call", "parse_body", "parse_output", "parse_query",
]
