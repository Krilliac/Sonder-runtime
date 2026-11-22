"""SPEC-5 thin routes (``POST /v1/recall``, ``POST /v1/outcome``) at the HTTP edge.

``serve.py`` has already read, size-checked and parsed the request body with
``_read_json`` before it dispatches here, so the parsed object is passed
through.  Reading the socket a second time would block an honest request for
the whole read timeout (its body is already gone) and would hand a client that
sends the body twice the duplicate instead of the body that was validated.

Each thin route reaches the global memory store, so it is gated under the
permission name of the operation it performs, exactly as a ``/<tool>``
spelling is: ``tool_refusal`` is serve.py's ``_http_tool_refusal``, which
applies the hosted-account global memory refusal, the system-operation
authority check, the role tool gate and the unattended permission decision,
in that order.  A thin handler mounted on a path with no bound name is
refused rather than served ungated.

State: none.  ``ROUTE_TOOLS`` is an immutable module constant; every call
works only on its arguments, so this is safe from any handler thread.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable

# The permission name each thin route is decided under. A recall
# (``RecallHandler`` -> ``MemoryFacade.recall``) pages past interaction
# episodes for a caller-chosen project out of the memory store and writes
# nothing; no catalogued tool performs that read, so it has its own name,
# graded read-only (``safe``, like ``memory_search``) in
# ``permission_modes.HTTP_ROUTE_WORK`` and listed in serve.py's
# ``_ACCOUNT_GLOBAL_MEMORY_TOOLS``. It is not ``apply_learned``: that tool's
# rules, grade and refusal receipts belong to a different operation. An
# outcome is ``/record_outcome`` with the same two arguments.
ROUTE_TOOLS = MappingProxyType({
    "/v1/recall": "recall",
    "/v1/outcome": "record_outcome",
})


@dataclass(frozen=True)
class ThinRequest:
    """The ``RequestLike`` a thin handler reads (``handlers.RequestLike``)."""

    method: str
    path: str
    body: bytes
    headers: dict[str, str]


def _forbidden(message: str) -> dict[str, Any]:
    # The thin handlers' own error shape (``handlers.error_response``).
    return {"error": "FORBIDDEN", "message": message, "retryable": False}


def serve(handler: Any, thin_handler: Any, path: str, req: Any, context: dict[str, Any], *,
          tool_refusal: Callable[..., str]) -> None:
    """Authorize, then run ``thin_handler`` on the already-parsed body ``req``."""
    if not context["authorized"]:
        handler._send_auth_error()
        return
    tool = ROUTE_TOOLS.get(path)
    if tool is None:
        refusal = "refused %s: this route is not bound to a gated tool" % path
    else:
        refusal = tool_refusal((tool,), path, context=context)
    if refusal:
        handler._send_json_payload(_forbidden(refusal), status=403)
        return
    headers = {key: value for key, value in handler.headers.items()}
    body = json.dumps(req).encode("utf-8")
    response = thin_handler.handle(ThinRequest("POST", path, body, headers))
    handler._send_json_payload(response.body, status=response.status)


__all__ = ["ROUTE_TOOLS", "ThinRequest", "serve"]
