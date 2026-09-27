"""Admin HTTP projection of the host developer-tool inventory.

``GET /v1/tools/inventory`` reads the cached (or lazily discovered) snapshot
with optional ``category``/``name`` filters.  ``POST
/v1/tools/inventory/refresh`` forces rediscovery.  Only the filters and the
``full`` flag are caller-controlled; tool names, argv, paths and environment
all come from the host-owned registry.  Paths in responses are redacted.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
from threading import BoundedSemaphore
from typing import Callable

from sonder_runtime.application.errors import InvalidInput
from sonder_runtime.application.host_tools import TOOL_NAME_PATTERN, ToolCategory, view_to_wire

MAX_RESPONSE_BYTES = 256 * 1024
_ALLOWED_QUERY_KEYS = frozenset({"category", "name"})
_TOOL_INVENTORY_SLOTS = BoundedSemaphore(2)


@contextmanager
def tool_inventory_request_slot():
    """Non-blocking admission: at most two inventory requests at a time."""
    slots = _TOOL_INVENTORY_SLOTS
    admitted = slots.acquire(blocking=False)
    try:
        yield admitted
    finally:
        if admitted:
            slots.release()


def _error(status: int, code: str) -> tuple[int, dict]:
    return status, {"error": {"code": code}}


def _render(view) -> tuple[int, dict]:
    body = view_to_wire(view)
    if len(json.dumps(body, ensure_ascii=True).encode("utf-8")) > MAX_RESPONSE_BYTES:
        return 413, {"error": {"code": "TOOL_INVENTORY_TOO_LARGE",
                               "message": "filter by category or name"}}
    return 200, body


def _service(service_factory: Callable[[], object] | None):
    if service_factory is None or not callable(service_factory):
        return None
    return service_factory()


def _parse_query(query: dict[str, list[str]]) -> tuple[str | None, str | None]:
    if not isinstance(query, dict) or set(query) - _ALLOWED_QUERY_KEYS:
        raise InvalidInput("unknown inventory query parameter")
    if any(not isinstance(value, list) or len(value) != 1 for value in query.values()):
        raise InvalidInput("each inventory query parameter may appear once")
    category = query.get("category", [None])[0]
    name = query.get("name", [None])[0]
    if category is not None:
        if not isinstance(category, str):
            raise InvalidInput("category must be a string")
        try:
            ToolCategory(category)
        except ValueError:
            raise InvalidInput("unknown tool category") from None
    if name is not None and (not isinstance(name, str) or not TOOL_NAME_PATTERN.match(name)):
        raise InvalidInput("invalid tool name")
    return category, name


def dispatch_tool_inventory(
    service_factory: Callable[[], object] | None,
    query: dict[str, list[str]],
) -> tuple[int, dict]:
    try:
        category, name = _parse_query(query)
    except (InvalidInput, ValueError, TypeError):
        return _error(400, "INVALID_TOOL_INVENTORY_QUERY")
    try:
        service = _service(service_factory)
        if service is None:
            return _error(503, "TOOL_INVENTORY_UNAVAILABLE")
        view = service.view(category=category, name=name, refresh=False, redacted=True)
        return _render(view)
    except PermissionError:
        return _error(403, "FORBIDDEN")
    except (InvalidInput, ValueError, TypeError):
        return _error(400, "INVALID_TOOL_INVENTORY_QUERY")
    except Exception:
        return _error(503, "TOOL_INVENTORY_UNAVAILABLE")


def dispatch_tool_inventory_refresh(
    service_factory: Callable[[], object] | None,
    payload: dict,
) -> tuple[int, dict]:
    if not isinstance(payload, dict) or set(payload) - {"full"}:
        return _error(400, "INVALID_TOOL_INVENTORY_QUERY")
    full = payload.get("full", False)
    if type(full) is not bool:
        return _error(400, "INVALID_TOOL_INVENTORY_QUERY")
    try:
        service = _service(service_factory)
        if service is None:
            return _error(503, "TOOL_INVENTORY_UNAVAILABLE")
        service.snapshot(refresh=True, full=full)
        view = service.view(refresh=False, redacted=True)
        return _render(view)
    except PermissionError:
        return _error(403, "FORBIDDEN")
    except (InvalidInput, ValueError, TypeError):
        return _error(400, "INVALID_TOOL_INVENTORY_QUERY")
    except Exception:
        return _error(503, "TOOL_INVENTORY_UNAVAILABLE")


__all__ = [
    "MAX_RESPONSE_BYTES",
    "dispatch_tool_inventory",
    "dispatch_tool_inventory_refresh",
    "tool_inventory_request_slot",
]
