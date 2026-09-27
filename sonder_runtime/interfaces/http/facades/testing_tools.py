"""HTTP projection of the structured test runs and the output digest.

Every route is one typed tool call through the runtime's typed gateway (see
``typed_gateway.py``), as the authenticated principal with ``source="http"``,
so the permission modes grade it unattended exactly as they grade
``/v1/build/*``: ``test_run`` is execution, ``test_run_result`` and
``output_digest`` are safe reads. Ownership is the services' own: a run or a
digested job that belongs to another principal is ``JOB_NOT_FOUND``.

Routes::

    POST /v1/tools/test-run              -> 202 {job_id} while running, 200 report
         body {project, runner, selector, timeout_seconds, workers, wait_seconds}
    GET  /v1/tools/test-run/{id}?wait_seconds=
                                         -> 202 status while running, 200 report
    POST /v1/tools/output-digest         body exactly one of {job_id, path},
                                         plus tail_lines, max_failure_lines

Cancel stays on ``POST /v1/jobs/{id}/cancel``; there is no second cancel path.
The facade only parses and maps; the handler in ``serve.py`` authenticates,
derives the principal and roots, and sends the response.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Mapping

from ....application.errors import InvalidInput
from .typed_gateway import (
    GatewayErrorCodes,
    MethodNotAllowed,
    UnknownRoute,
    error_response,
    execute_typed_call,
    parse_body,
    parse_query,
)

TEST_RUN_ROUTE = "/v1/tools/test-run"
OUTPUT_DIGEST_ROUTE = "/v1/tools/output-digest"
MAX_RESPONSE_BYTES = 64 * 1024

_RESULT_ROUTE = re.compile(r"^/v1/tools/test-run/(test-run-[0-9a-f]{32})$")
_RUN_BODY = frozenset({"project", "runner", "selector", "timeout_seconds", "workers",
                       "wait_seconds"})
_DIGEST_BODY = frozenset({"job_id", "path", "tail_lines", "max_failure_lines"})
_WAIT_QUERY = {"wait_seconds": int}

# error_code -> HTTP status for a typed tool failure.
_STATUS_BY_CODE = {
    "JOB_NOT_FOUND": 404,
    "DEVELOPER_TOOLS_UNAVAILABLE": 503,
    "DEPENDENCY_UNAVAILABLE": 503,
    "RUNNER_UNAVAILABLE": 503,
    "TEST_RUN_BUSY": 429,
    "PROJECT_OUTSIDE_ROOTS": 403,
    "SELECTOR_ESCAPES_PROJECT": 403,
    "DIGEST_SOURCE_REJECTED": 403,
    "CTEST_BUILD_TREE_MISSING": 404,
    "HOST_IO_FAILURE": 500,
}

PERMISSION_REMEDIES = (
    "add a permission allow rule for test_run scoped to the project",
    "switch the permission mode to auto (test_run is execution)",
    "run /test from the operator console, which can ask",
    "approve this exact call once with its call_id (permission_approve)",
)

GATEWAY_CODES = GatewayErrorCodes(
    request_prefix="http-test-",
    unavailable="DEVELOPER_TOOLS_UNAVAILABLE",
    invalid="INVALID_TEST_REQUEST",
    abandoned="TEST_REQUEST_ABANDONED",
    too_large="TEST_RESPONSE_TOO_LARGE",
    failed="TEST_TOOL_FAILED",
    status_by_code=_STATUS_BY_CODE,
    remedies=PERMISSION_REMEDIES,
    max_response_bytes=MAX_RESPONSE_BYTES,
)


def owns(path: str) -> bool:
    return isinstance(path, str) and (
        path in (TEST_RUN_ROUTE, OUTPUT_DIGEST_ROUTE) or path.startswith(TEST_RUN_ROUTE + "/"))


def route_call(method: str, path: str, query: Mapping[str, list[str]],
               payload: Any) -> tuple[str, dict] | None:
    """Map one request to ``(tool_name, arguments)``.

    Returns None for a path this facade does not own; raises ``InvalidInput``
    for a malformed request on a path it owns.
    """
    if not owns(path):
        return None
    method = str(method or "").upper()
    if path == TEST_RUN_ROUTE:
        if method != "POST":
            raise MethodNotAllowed()
        parse_query(query, {})
        return "test_run", parse_body(payload, _RUN_BODY)
    if path == OUTPUT_DIGEST_ROUTE:
        if method != "POST":
            raise MethodNotAllowed()
        parse_query(query, {})
        body = parse_body(payload, _DIGEST_BODY)
        if bool(body.get("job_id")) == bool(body.get("path")):
            raise InvalidInput("give exactly one of job_id or path")
        return "output_digest", body
    match = _RESULT_ROUTE.fullmatch(path)
    if match is None:
        raise UnknownRoute()
    if method != "GET":
        raise MethodNotAllowed()
    return "test_run_result", {"job_id": match.group(1), **parse_query(query, _WAIT_QUERY)}


class TestRunHttpRoutes:
    """``/v1/tools/test-run`` and ``/v1/tools/output-digest`` over the typed gateway."""

    __test__ = False  # not a pytest test class

    def __init__(self, tools_getter: Callable[[], Any]) -> None:
        self._tools_getter = tools_getter

    @staticmethod
    def owns(path: str) -> bool:
        return owns(path)

    def dispatch(self, method: str, path: str, query: Mapping[str, list[str]], payload: Any, *,
                 principal_id: str, workspace_roots: tuple[str, ...] = (),
                 auth_level: str = "user", deadline_monotonic: float | None = None,
                 ) -> tuple[int, dict]:
        try:
            call = route_call(method, path, query, payload)
        except MethodNotAllowed:
            return error_response(405, "METHOD_NOT_ALLOWED")
        except UnknownRoute:
            return error_response(404, "NOT_FOUND")
        except (InvalidInput, ValueError, TypeError) as exc:
            return error_response(400, "INVALID_TEST_REQUEST", str(exc))
        if call is None:
            return error_response(404, "NOT_FOUND")
        tool, arguments = call
        status, body = execute_typed_call(
            self._tools_getter, tool, arguments, GATEWAY_CODES, principal_id=principal_id,
            workspace_roots=workspace_roots, auth_level=auth_level,
            deadline_monotonic=deadline_monotonic,
        )
        if status == 200 and body.get("object") == "test_run_status":
            return 202, body
        return status, body


__all__ = [
    "GATEWAY_CODES", "OUTPUT_DIGEST_ROUTE", "PERMISSION_REMEDIES", "TEST_RUN_ROUTE",
    "TestRunHttpRoutes", "owns", "route_call",
]
