"""HTTP projection of the C++ build tools (``/v1/build/*``).

Every route is one typed tool call through the runtime's typed gateway, as
the authenticated principal, with ``source="http"`` and the gateway deciding
permission (an HTTP caller has nobody at a console, so the modes grade it
unattended). There is no second path to the build services: the schema, the
resource policy, the permission modes, the build-fix grant, redaction and the
durable receipt all apply exactly as for the native MCP and REPL surfaces.

Routes::

    GET  /v1/build/model?project=&build_dir=&preset=&detail=&target=&max_items=&refresh=
    POST /v1/build/jobs                    -> 202 {job_id} while running, 200 report
    GET  /v1/build/jobs/{id}?wait_seconds=
    POST /v1/build/jobs/{id}/cancel
    POST /v1/build/fix                     -> 202 {job_id}
    GET  /v1/build/fix/{id}?wait_seconds=
    POST /v1/build/fix/{id}/cancel
    POST /v1/build/fix/{id}/restore        body {"files": [...]} (optional)

The facade only parses and maps; the handler in ``serve.py`` authenticates,
derives the principal and roots, and sends the response.
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any, Callable, Mapping

from ....application.errors import Cancelled, DeadlineExceeded, Forbidden, InvalidInput
from ....application.tools.gateway_contract import (
    ToolGatewayRequest,
    ToolPermission,
    ToolScope,
)

BUILD_ROUTE_PREFIX = "/v1/build/"
MAX_RESPONSE_BYTES = 64 * 1024
MAX_BODY_KEYS = 32

_JOB_ROUTE = re.compile(r"^/v1/build/jobs/(build-job-[0-9a-f]{16,32})(/cancel)?$")
_FIX_ROUTE = re.compile(r"^/v1/build/fix/(build-fix-[0-9a-f]{16,32})(/cancel|/restore)?$")

_MODEL_QUERY = {"project": str, "build_dir": str, "preset": str, "detail": str,
                "target": str, "max_items": int, "refresh": bool}
_WAIT_QUERY = {"wait_seconds": int}
_JOB_BODY = frozenset({
    "project", "build_dir", "action", "target", "config", "platform", "preset",
    "build_preset", "file", "generator", "profile", "jobs", "timeout_seconds",
    "wait_seconds", "allow_network",
})
_FIX_BODY = frozenset({
    "project", "build_dir", "target", "config", "platform", "focus_file", "attempts",
    "apply", "revert_after", "editable_globs", "timeout_seconds", "verify_dependents",
    "wait_seconds", "allow_network",
})

# error_code -> HTTP status for a typed tool failure.
_STATUS_BY_CODE = {
    "JOB_NOT_FOUND": 404,
    "BUILD_TOOLS_UNAVAILABLE": 503,
    "BUILD_MODEL_UNAVAILABLE": 503,
    "RUNNER_UNAVAILABLE": 503,
    "ENV_CAPTURE_FAILED": 503,
    "NETWORK_ISOLATION_UNAVAILABLE": 503,
    "BUILD_DIR_BUSY": 409,
    "RESTORE_CONFLICT": 409,
    "BUILD_BUSY": 429,
    "PROJECT_OUTSIDE_ROOTS": 403,
    "BUILD_TREE_REJECTED": 403,
    "UTILITY_TARGET_REFUSED": 403,
    "RESIDENCY_REFUSED": 403,
    "BUILD_TREE_MISSING": 404,
    "HOST_IO_FAILURE": 500,
}

PERMISSION_REMEDIES = (
    "add a permission allow rule for this tool scoped to the project",
    "switch the permission mode to acceptEdits or auto (build_job and build_fix need auto)",
    "run the command from the operator console, which can ask",
    "approve this exact call once with its call_id (permission_approve)",
)


def _error(status: int, code: str, message: str = "", **extra) -> tuple[int, dict]:
    body: dict[str, Any] = {"error": {"code": code}}
    if message:
        body["error"]["message"] = str(message)[:400]
    body["error"].update(extra)
    return status, body


def _one(query: Mapping[str, list[str]], allowed: Mapping[str, type]) -> dict:
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


def _body(payload: Any, allowed: frozenset[str]) -> dict:
    if payload is None:
        return {}
    if not isinstance(payload, dict) or len(payload) > MAX_BODY_KEYS:
        raise InvalidInput("request body must be a JSON object")
    unknown = set(payload) - allowed
    if unknown:
        raise InvalidInput("unknown field: %s" % sorted(unknown)[0][:40])
    return dict(payload)


def route_call(method: str, path: str, query: Mapping[str, list[str]],
               payload: Any) -> tuple[str, dict] | None:
    """Map one ``/v1/build/*`` request to ``(tool_name, arguments)``.

    Returns None for a path this facade does not own; raises ``InvalidInput``
    for a malformed request on a path it owns.
    """
    if not isinstance(path, str) or not path.startswith(BUILD_ROUTE_PREFIX):
        return None
    method = str(method or "").upper()
    if path == "/v1/build/model":
        if method != "GET":
            raise _MethodNotAllowed()
        return "build_model", _one(query, _MODEL_QUERY)
    if path == "/v1/build/jobs":
        if method != "POST":
            raise _MethodNotAllowed()
        _one(query, {})
        return "build_job", _body(payload, _JOB_BODY)
    if path == "/v1/build/fix":
        if method != "POST":
            raise _MethodNotAllowed()
        _one(query, {})
        return "build_fix", _body(payload, _FIX_BODY)
    match = _JOB_ROUTE.fullmatch(path)
    if match is not None:
        job_id, action = match.group(1), match.group(2)
        if action == "/cancel":
            if method != "POST":
                raise _MethodNotAllowed()
            _one(query, {})
            _body(payload, frozenset())
            return "build_job_result", {"job_id": job_id, "cancel": True}
        if method != "GET":
            raise _MethodNotAllowed()
        return "build_job_result", {"job_id": job_id, **_one(query, _WAIT_QUERY)}
    match = _FIX_ROUTE.fullmatch(path)
    if match is not None:
        job_id, action = match.group(1), match.group(2)
        if action == "/cancel":
            if method != "POST":
                raise _MethodNotAllowed()
            _one(query, {})
            _body(payload, frozenset())
            return "build_fix_result", {"job_id": job_id, "cancel": True}
        if action == "/restore":
            if method != "POST":
                raise _MethodNotAllowed()
            _one(query, {})
            body = _body(payload, frozenset({"files"}))
            return "build_fix_restore", {"job_id": job_id, **body}
        if method != "GET":
            raise _MethodNotAllowed()
        return "build_fix_result", {"job_id": job_id, **_one(query, _WAIT_QUERY)}
    raise _UnknownRoute()


class _MethodNotAllowed(Exception):
    pass


class _UnknownRoute(Exception):
    pass


def _parse_output(output: Any) -> dict:
    if isinstance(output, Mapping):
        return dict(output)
    try:
        value = json.loads(output) if isinstance(output, str) and output else {}
    except ValueError:
        return {"output": str(output)[:2000]}
    return value if isinstance(value, dict) else {"output": value}


def _status_for_success(tool: str, body: Mapping[str, Any]) -> int:
    if tool in ("build_job", "build_fix") and body.get("status") in ("running", "pending", None) \
            and "job_id" in body and str(body.get("object") or "").endswith("status"):
        return 202
    return 200


class BuildHttpRoutes:
    """``/v1/build/*`` over the typed gateway the getter returns."""

    def __init__(self, tools_getter: Callable[[], Any]) -> None:
        self._tools_getter = tools_getter

    def owns(self, path: str) -> bool:
        return isinstance(path, str) and path.startswith(BUILD_ROUTE_PREFIX)

    def dispatch(self, method: str, path: str, query: Mapping[str, list[str]], payload: Any, *,
                 principal_id: str, workspace_roots: tuple[str, ...] = (),
                 auth_level: str = "user", deadline_monotonic: float | None = None,
                 ) -> tuple[int, dict]:
        try:
            call = route_call(method, path, query, payload)
        except _MethodNotAllowed:
            return _error(405, "METHOD_NOT_ALLOWED")
        except _UnknownRoute:
            return _error(404, "NOT_FOUND")
        except (InvalidInput, ValueError, TypeError) as exc:
            return _error(400, "INVALID_BUILD_REQUEST", str(exc))
        if call is None:
            return _error(404, "NOT_FOUND")
        tool, arguments = call
        tools = self._tools_getter() if callable(self._tools_getter) else None
        if tools is None:
            return _error(503, "BUILD_TOOLS_UNAVAILABLE", "the typed tool gateway is not composed")
        descriptor = tools.graph.registry.get(tool)
        if descriptor is None:
            return _error(503, "BUILD_TOOLS_UNAVAILABLE", "the build tools are not registered")
        effects = frozenset(effect.name.lower() for effect in descriptor.effects)
        try:
            request = ToolGatewayRequest(
                request_id="http-build-" + uuid.uuid4().hex,
                tool_name=tool,
                arguments=arguments,
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
                code = str(decision.get("error_code") or "INVALID_BUILD_REQUEST")
                return _error(_STATUS_BY_CODE.get(code, 400), code, str(exc))
            return _error(403, "PERMISSION_DENIED", str(exc), decision=decision,
                          remedies=list(PERMISSION_REMEDIES))
        except (Cancelled, DeadlineExceeded) as exc:
            return _error(503, "BUILD_REQUEST_ABANDONED", type(exc).__name__)
        except (InvalidInput, ValueError, TypeError) as exc:
            return _error(400, "INVALID_BUILD_REQUEST", str(exc))
        body = _parse_output(receipt.output)
        if not receipt.success:
            code = str(receipt.error_code or body.get("error_code") or "BUILD_FAILED")
            return _error(_STATUS_BY_CODE.get(code, 400), code,
                          str(body.get("message") or receipt.error or ""))
        body.setdefault("ok", True)
        body["receipt"] = {"request_id": receipt.request_id, "policy_match": receipt.policy_match}
        if len(json.dumps(body, ensure_ascii=True).encode("utf-8")) > MAX_RESPONSE_BYTES:
            return _error(413, "BUILD_RESPONSE_TOO_LARGE", "narrow the request")
        return _status_for_success(tool, body), body


__all__ = ["BUILD_ROUTE_PREFIX", "BuildHttpRoutes", "PERMISSION_REMEDIES", "route_call"]
