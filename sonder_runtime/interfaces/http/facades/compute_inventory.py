"""Bounded host-authorized compute inventory and page refresh projection."""
import json
import re
from contextlib import contextmanager
from threading import BoundedSemaphore

_INVENTORY_REQUEST_SLOTS = BoundedSemaphore(8)


@contextmanager
def inventory_request_slot():
    """Bound admitted inventory bodies/projections through response delivery."""
    slots = _INVENTORY_REQUEST_SLOTS
    admitted = slots.acquire(blocking=False)
    try:
        yield admitted
    finally:
        if admitted:
            slots.release()


def dispatch_compute_inventory(factory, query):
    if not callable(factory):
        return 503, {"error": {"code": "COMPUTE_INVENTORY_UNAVAILABLE"}}
    if not isinstance(query, dict) or set(query) - {"limit", "cursor"}:
        return 400, {"error": {"code": "INVALID_INVENTORY_QUERY"}}
    if any(not isinstance(value, list) or len(value) != 1 for value in query.values()):
        return 400, {"error": {"code": "INVALID_INVENTORY_QUERY"}}
    limit = query.get("limit", ["32"])[0]
    cursor = query.get("cursor", [None])[0]
    if not isinstance(limit, str) or not re.fullmatch(r"[0-9]{1,2}", limit) or not 1 <= int(limit) <= 64:
        return 400, {"error": {"code": "INVALID_INVENTORY_QUERY"}}
    if cursor is not None and (not isinstance(cursor, str) or not 1 <= len(cursor) <= 1024):
        return 400, {"error": {"code": "INVALID_INVENTORY_QUERY"}}
    try:
        page = factory(limit=int(limit), cursor=cursor)
        body = {"object": "compute_inventory_page", **page}
        if len(json.dumps(body, ensure_ascii=True).encode("utf-8")) > 256 * 1024:
            return 413, {"error": {"code": "INVENTORY_PAGE_TOO_LARGE", "message": "request a smaller limit"}}
        return 200, body
    except PermissionError:
        return 403, {"error": {"code": "FORBIDDEN"}}
    except (ValueError, TypeError):
        return 400, {"error": {"code": "INVALID_INVENTORY_QUERY"}}
    except Exception:
        return 503, {"error": {"code": "COMPUTE_INVENTORY_UNAVAILABLE"}}


def dispatch_compute_refresh(factory, payload):
    if not isinstance(payload, dict) or set(payload) - {"limit", "cursor"}:
        return 400, {"error": {"code": "INVALID_INVENTORY_QUERY"}}
    limit = payload.get("limit", 32)
    if type(limit) is not int:
        return 400, {"error": {"code": "INVALID_INVENTORY_QUERY"}}
    query = {"limit": [str(limit)]}
    if "cursor" in payload:
        query["cursor"] = [payload["cursor"]]
    status, body = dispatch_compute_inventory(factory, query)
    if status == 403:
        body = {"error": {"code": "REMOTE_COMPUTE_DISABLED"}}
    elif status == 200:
        body["object"] = "compute_refresh_page"
    return status, body
