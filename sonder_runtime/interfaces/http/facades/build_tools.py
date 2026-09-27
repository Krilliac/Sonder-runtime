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

import re
from typing import Any, Callable, Mapping

from ....application.errors import InvalidInput
from .typed_gateway import (
    GatewayErrorCodes,
    MethodNotAllowed as _MethodNotAllowed,
    UnknownRoute as _UnknownRoute,
    error_response as _error,
    execute_typed_call,
    parse_body as _body,
    parse_query as _one,
)

BUILD_ROUTE_PREFIX = "/v1/build/"
MAX_RESPONSE_BYTES = 64 * 1024

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

GATEWAY_CODES = GatewayErrorCodes(
    request_prefix="http-build-",
    unavailable="BUILD_TOOLS_UNAVAILABLE",
    invalid="INVALID_BUILD_REQUEST",
    abandoned="BUILD_REQUEST_ABANDONED",
    too_large="BUILD_RESPONSE_TOO_LARGE",
    failed="BUILD_FAILED",
    status_by_code=_STATUS_BY_CODE,
    remedies=PERMISSION_REMEDIES,
    max_response_bytes=MAX_RESPONSE_BYTES,
)


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
        status, body = execute_typed_call(
            self._tools_getter, tool, arguments, GATEWAY_CODES, principal_id=principal_id,
            workspace_roots=workspace_roots, auth_level=auth_level,
            deadline_monotonic=deadline_monotonic,
        )
        if status != 200:
            return status, body
        return _status_for_success(tool, body), body


__all__ = [
    "BUILD_ROUTE_PREFIX", "BuildHttpRoutes", "GATEWAY_CODES", "PERMISSION_REMEDIES", "route_call",
]
