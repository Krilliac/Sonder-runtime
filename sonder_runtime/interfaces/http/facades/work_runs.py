"""HTTP work-run routes: a principal's durable work runs, and cancel.

* ``GET /v1/work-runs`` -- the caller's recent runs;
* ``GET /v1/work-runs/<id>`` -- one of the caller's runs;
* ``POST /v1/work-runs/<id>/cancel`` -- request cancellation.

Runs are visible only to the principal that started them, and the routes
need developer or admin authority.  Cancel stops the run's effects (files,
programs, destructive tools) at the next attempt; model steps already
admitted finish to their step bound.

``handler`` is serve.py's request handler (auth check and JSON sender); the
work runner, the authority check, the principal derivation, the store's
failure types and the logger are injected by ``serve.py``.
"""
from __future__ import annotations

import re
from typing import Any, Callable

ROUTE = "/v1/work-runs"


def serve_request(handler: Any, method: str, path: str, context: Any, *, runner: Any,
                  developer_authorized: Callable[[Any], bool],
                  principal_of: Callable[[Any], str],
                  store_errors: tuple[type[BaseException], ...], log: Any) -> bool:
    """Serve one work-run request; ``False`` for another route.

    ``context`` is the request's auth context, or ``None`` to read it from
    ``handler``.
    """
    route = path.rstrip("/")
    if route != "/v1/work-runs" and not route.startswith("/v1/work-runs/"):
        return False
    parts = route[len("/v1/work-runs"):].strip("/").split("/") if route != "/v1/work-runs" else []
    if context is None:
        context = handler._request_auth_context()
    if not context["authorized"]:
        handler._send_auth_error()
        return True
    if not developer_authorized(context):
        handler._send_json_payload({"error": {
            "message": "developer or admin authentication is required for work runs",
            "type": "forbidden", "code": "FORBIDDEN"}}, status=403)
        return True
    run_id = parts[0] if parts else ""
    if run_id and not re.fullmatch(r"wr-[0-9a-f]{32}", run_id):
        handler._send_json_payload({"error": {"message": "invalid work run id",
                                              "type": "invalid_request"}}, status=400)
        return True
    principal = principal_of(context)
    try:
        if method == "GET" and not parts:
            handler._send_json_payload({"runs": runner.recent(principal)},
                                       headers={"Cache-Control": "no-store"})
            return True
        if method == "GET" and len(parts) == 1:
            record = runner.get(run_id, principal)
        elif method == "POST" and len(parts) == 2 and parts[1] == "cancel":
            record = runner.cancel(run_id, principal)
        else:
            handler._send_json_payload({"error": {"message": "method not allowed",
                                                  "type": "invalid_request"}}, status=405)
            return True
    except store_errors:
        log.error("work run store unavailable", exc_info=True)
        handler._send_json_payload({"error": {"message": "work run store unavailable",
                                              "type": "server_error",
                                              "code": "WORK_RUN_STORE_UNAVAILABLE"}},
                                   status=503, headers={"Retry-After": "1"})
        return True
    if record is None:
        handler._send_json_payload({"error": {"message": "work run not found",
                                              "type": "not_found", "code": "NOT_FOUND"}},
                                   status=404)
        return True
    handler._send_json_payload(record, headers={"Cache-Control": "no-store"})
    return True


__all__ = ["ROUTE", "serve_request"]
