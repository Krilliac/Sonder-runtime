"""Root-free HTTP routing for the typed durable-session facade."""
from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence

from ....application.session.http_facade import HttpSessionFacade, HttpSessionResult

logger = logging.getLogger(__name__)


def dispatch_session_route(
    facade: HttpSessionFacade, path: str, *, query: Mapping[str, Sequence[str]] | None = None
) -> HttpSessionResult | None:
    """Dispatch one bounded read-only durable-session route."""
    route = path.split("?", 1)[0].rstrip("/")
    prefix = "/v1/sessions/"
    if not route.startswith(prefix):
        return None
    parts = route[len(prefix):].split("/")
    if len(parts) != 2 or not all(parts):
        logger.debug(f"dispatch_session_route: malformed path={path!r}")
        return HttpSessionResult(404, {"error": "not_found"})
    session_id, operation = parts[0], parts[1]
    if not session_id or "/" in session_id or "\\" in session_id:
        return HttpSessionResult(404, {"error": "not_found"})
    logger.debug(f"dispatch_session_route: session_id={session_id!r}, operation={operation!r}")
    values = query or {}

    def one(name: str, default: str | None = None) -> str | None:
        candidates = values.get(name)
        return candidates[0] if candidates and len(candidates) == 1 else default

    def integer(name: str, default: int | None = None) -> int | None:
        value = one(name)
        if value is None or value == "":
            return default
        try:
            return int(value)
        except ValueError:
            raise ValueError(f"{name} must be an integer") from None

    try:
        if operation == "events":
            return facade.read(
                session_id,
                event_type=one("event_type"), text=one("text"),
                page_size=integer("page_size", 100) or 100,
                cursor=one("cursor"),
                start_sequence=integer("start_sequence", 1) or 1,
                end_sequence=integer("end_sequence"),
            )
        if operation == "export":
            return facade.export(
                session_id,
                start_sequence=integer("start_sequence", 1) or 1,
                end_sequence=integer("end_sequence"),
                max_events=integer("max_events", 1_000) or 1_000,
            )
        if operation == "replay":
            return facade.replay(session_id, max_events=integer("max_events"))
        if operation == "trajectory":
            return facade.trajectory(session_id, max_events=integer("max_events", 1_000) or 1_000)
        if operation == "repair":
            return facade.repair(session_id)
        if operation == "fork":
            sequence = integer("fork_sequence")
            if sequence is None:
                return HttpSessionResult(400, {"error": "invalid_session_query"})
            return facade.fork(session_id, fork_sequence=sequence,
                               child_session_id=one("child_session_id"))
        if operation == "checkpoint":
            return facade.checkpoint(session_id)
    except ValueError:
        logger.warning(f"session route rejected: invalid query params for session_id={session_id!r}, operation={operation!r}")
        logger.debug(f"dispatch_session_route: invalid query params for session_id={session_id!r}, operation={operation!r}")
        return HttpSessionResult(400, {"error": "invalid_session_query"})
    logger.debug(f"dispatch_session_route: unrecognized operation={operation!r}")
    return HttpSessionResult(404, {"error": "not_found"})


__all__ = ["dispatch_session_route"]
