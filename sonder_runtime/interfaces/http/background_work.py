"""Read-only, authenticated aggregation for the Agents surface.

The serving host supplies the three already-scoped status providers.  Keeping
the aggregation here means the HTTP boundary does not need to import the
legacy ``server`` module (or any persistence adapter), and makes the response
safe to exercise with small in-memory providers.
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

PREVIEW_CHARS = 240


@dataclass(frozen=True)
class BackgroundWorkHttpResult:
    body: dict[str, Any]
    status_code: int = 200


def _text(value: Any, limit: int = PREVIEW_CHARS) -> str:
    return str(value or "")[:limit]


def _stamp(row: dict[str, Any], *names: str) -> float:
    for name in names:
        try:
            value = float(row.get(name) or 0)
        except (TypeError, ValueError):
            value = 0.0
        if value > 0:
            return value
    return 0.0


def _integer(value: Any, default: int = 0) -> int:
    """Read bounded numeric metadata without letting a corrupt row abort a page."""
    try:
        if isinstance(value, bool):
            return default
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return default


def _elapsed(row: dict[str, Any], now: float) -> float:
    started = _stamp(row, "started_ts", "created_ts", "created_at")
    ended = _stamp(row, "finished_ts", "completed_ts")
    if not ended and str(row.get("status") or "") in {
        "done", "completed", "failed", "cancelled", "interrupted",
    }:
        ended = _stamp(row, "updated_ts", "updated_at")
    return round(max(0.0, (ended or now) - started), 3) if started else 0.0


def _preview(row: dict[str, Any]) -> str:
    return _text(
        row.get("summary") or row.get("output") or row.get("error")
        or row.get("activity")
    )


def _status_bucket(status: Any) -> str:
    value = str(status or "").lower()
    if value in {"done", "completed", "complete", "passed", "success", "succeeded"}:
        return "done"
    if value in {"queued", "pending", "ready", "planned", "todo"}:
        return "queued"
    if value in {"running", "active", "planning", "executing"}:
        return "running"
    if value in {"failed", "error"}:
        return "failed"
    if value in {"cancelled", "canceled", "interrupted"}:
        return "cancelled"
    return "other"


def _lane(row: dict[str, Any], now: float) -> dict[str, Any]:
    return {
        "id": str(row.get("id") or row.get("lane_id") or ""),
        "task": _text(row.get("task"), 2_000),
        "status": str(row.get("status") or "unknown"),
        "project": str(row.get("project") or ""),
        "updated_ts": _stamp(row, "updated_ts", "updated_at", "created_ts"),
        "created_order": _integer(row.get("created_order")),
        "elapsed_seconds": _elapsed(row, now),
        "preview": _preview(row),
        "cancelable": str(row.get("status") or "") not in {
            "completed", "failed", "cancelled", "archived",
        },
    }


def _sort_lanes(rows: list[dict[str, Any]], now: float) -> list[dict[str, Any]]:
    """Lane persistence exposes insertion position, not a timestamp.

    The service returns position ascending, so reverse that bounded page when
    no provider supplied a real timestamp.  Providers with timestamps retain
    timestamp ordering below.
    """
    public = [_lane(row, now) for row in rows]
    if any(row["created_order"] for row in public):
        return sorted(public, key=lambda row: row["created_order"], reverse=True)
    if public and not any(row["updated_ts"] for row in public):
        return list(reversed(public))
    return sorted(public, key=lambda row: row["updated_ts"], reverse=True)


def _child(row: dict[str, Any], now: float) -> dict[str, Any]:
    return {
        "id": str(row.get("id") or ""),
        "task": _text(row.get("task"), 2_000),
        "status": str(row.get("status") or "unknown"),
        "activity": _text(row.get("activity")),
        "updated_ts": _stamp(row, "updated_ts", "started_ts"),
        "elapsed_seconds": _elapsed(row, now),
        "preview": _preview(row),
        "cancelable": str(row.get("status") or "") in {"queued", "running"},
    }


def _fleet(master: dict[str, Any], children: list[dict[str, Any]], now: float) -> dict[str, Any]:
    counts = {"done": 0, "running": 0, "queued": 0, "failed": 0,
              "cancelled": 0, "other": 0}
    public_children = []
    for row in sorted(children, key=lambda item: _stamp(item, "updated_ts", "started_ts"), reverse=True):
        counts[_status_bucket(row.get("status"))] += 1
        public_children.append(_child(row, now))
    if isinstance(master.get("child_counts"), dict):
        counts = dict.fromkeys(counts, 0)
        for status, count in master["child_counts"].items():
            counts[_status_bucket(status)] += _integer(count)
    requested = _integer(master.get("requested_agents"), len(children))
    slots = _integer(master.get("worker_slots"))
    return {
        "id": str(master.get("id") or ""),
        "task": _text(master.get("task"), 2_000),
        "status": str(master.get("status") or "unknown"),
        "project": str(master.get("project") or ""),
        "requested_agents": requested,
        "worker_slots": slots,
        "updated_ts": _stamp(master, "updated_ts", "started_ts", "created_ts"),
        "elapsed_seconds": _elapsed(master, now),
        "counts": counts,
        "children": public_children,
        "children_truncated": bool(master.get("children_truncated")),
        "created_ts": _stamp(master, "started_ts", "created_ts", "updated_ts"),
        "preview": _preview(master),
        "cancelable": str(master.get("status") or "") in {"queued", "running"},
    }


def _autopilot(row: dict[str, Any], now: float) -> dict[str, Any]:
    plan = row.get("plan")
    if plan is None and row.get("plan_json"):
        try:
            plan = json.loads(row["plan_json"])
        except (TypeError, ValueError):
            plan = []
    if not isinstance(plan, list):
        plan = []
    counts = {"total": len(plan), "done": 0, "running": 0, "queued": 0,
              "failed": 0, "cancelled": 0, "other": 0}
    for task in plan:
        status = task.get("status") if isinstance(task, dict) else "other"
        counts[_status_bucket(status)] += 1
    current = row.get("current_task")
    current_task = ""
    if isinstance(current, int) and 0 <= current < len(plan) and isinstance(plan[current], dict):
        current_task = _text(plan[current].get("title") or plan[current].get("task"), 2_000)
    elif isinstance(current, dict):
        current_task = _text(current.get("title") or current.get("task"), 2_000)
    return {
        "id": str(row.get("id") or ""),
        "objective": _text(row.get("objective"), 2_000),
        "status": str(row.get("status") or "unknown"),
        "phase": str(row.get("phase") or ""),
        "current_task": current_task,
        "task_counts": counts,
        "updated_ts": _stamp(row, "updated_ts", "created_ts"),
        "created_ts": _stamp(row, "created_ts", "started_ts", "updated_ts"),
        "elapsed_seconds": _elapsed(row, now),
        "preview": _preview(row),
        "cancelable": str(row.get("status") or "") not in {"completed", "failed", "cancelled"},
    }


class BackgroundWorkAggregator:
    """Compose already-authorized lane, fleet, and autopilot snapshots."""

    def __init__(
        self,
        *,
        lanes: Callable[..., dict],
        fleets: Callable[..., list[dict]],
        autopilot: Callable[..., list[dict]],
        owner_scope: Callable[[Any], str],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._lanes = lanes
        self._fleets = fleets
        self._autopilot = autopilot
        self._owner_scope = owner_scope
        self._clock = clock

    def snapshot(self, context: Any, *, project: str = "", limit: int = 100) -> dict[str, Any]:
        principal = str(getattr(context, "principal_id", "") or "")
        if not principal:
            raise PermissionError("authenticated principal is required")
        owner = str(self._owner_scope(context) or "")
        if not owner:
            raise PermissionError("request owner is required")
        cap = max(1, min(int(limit or 100), 200))
        now = self._clock()
        lane_result = self._lanes(context, limit=cap)
        lane_rows, lane_truncated = _rows_and_truncation(lane_result, "lanes")
        # Keep the provider call within the caller's bounded page.  A status
        # facade may additionally return ``masters`` alongside its child page;
        # the response says when that provider is incomplete.
        fleet_result = self._fleets(owner_id=owner, project=project, limit=cap)
        fleet_rows, fleet_truncated = _rows_and_truncation(fleet_result, "agents")
        auto_result = self._autopilot(request_owner=owner, limit=cap)
        auto_rows, auto_truncated = _rows_and_truncation(auto_result, "runs")
        masters = [row for row in fleet_rows if str(row.get("role") or "") == "master" and not row.get("parent_id")]
        fleets = []
        for master in masters:
            children = [row for row in fleet_rows if str(row.get("parent_id") or "") == str(master.get("id") or "")]
            fleets.append(_fleet(master, children, now))
        groups = {
            "lanes": _sort_lanes(lane_rows, now),
            "fleets": sorted(fleets, key=lambda row: row["created_ts"], reverse=True),
            "autopilot": sorted(
                (_autopilot(row, now) for row in auto_rows),
                key=lambda row: row["created_ts"], reverse=True,
            ),
        }
        return {
            "captured_at": now,
            "groups": groups,
            "truncated": {
                "lanes": lane_truncated,
                "fleets": fleet_truncated or any(row["children_truncated"] for row in fleets),
                "autopilot": auto_truncated,
            },
        }


def build_background_work_aggregator(
    *,
    lanes: Callable[..., dict],
    fleets: Callable[..., list[dict]],
    autopilot: Callable[..., list[dict]],
    owner_scope: Callable[[Any], str],
    clock: Callable[[], float] = time.time,
) -> BackgroundWorkAggregator:
    """Construct the read-only aggregate from host-owned status providers.

    The HTTP host should bind each provider to its authenticated operation
    context (and, for fleets and autopilot, the owner/project scope) before
    passing it here.  This keeps persistence and the legacy ``server`` module
    out of the packaged HTTP layer.
    """
    return BackgroundWorkAggregator(
        lanes=lanes,
        fleets=fleets,
        autopilot=autopilot,
        owner_scope=owner_scope,
        clock=clock,
    )


def runtime_background_work(application, auth, *, fleet_snapshot, autopilot_snapshot,
                            admin_authorized, request_owner):
    """Bind explicit persistence projections to the host's authenticated scope."""
    is_admin = bool(admin_authorized(auth))

    def lane_rows(context, *, limit=100):
        return application.agent_lanes().list(context, limit=limit, newest_first=True)

    def fleet_rows(*, owner_id="", project="", limit=100):
        # Legacy fleet ownership is a worker-process identity, not an HTTP
        # account. Only the existing administrator boundary may list it.
        return fleet_snapshot(limit=limit, project=project) if is_admin else []

    def autopilot_rows(*, request_owner="", limit=100):
        return autopilot_snapshot(limit=limit, request_owner=owner)

    owner = request_owner(auth)
    return build_background_work_aggregator(
        lanes=lane_rows, fleets=fleet_rows, autopilot=autopilot_rows,
        owner_scope=lambda context: context.principal_id,
    )


def _rows_and_truncation(value: Any, key: str) -> tuple[list[dict[str, Any]], bool]:
    """Accept list providers and paged provider envelopes without guessing."""
    if isinstance(value, dict):
        rows = value.get(key, [])
        # Status facades sometimes return masters separately from their child
        # page.  Keep that additive shape so a busy fleet cannot hide its
        # master row behind the first page of children.
        if key == "agents" and isinstance(value.get("masters"), list):
            rows = list(value.get("masters", [])) + (
                rows if isinstance(rows, list) else []
            )
        if not isinstance(rows, list):
            rows = []
        clean = [row for row in rows if isinstance(row, dict)]
        seen = set()
        deduped = []
        for row in clean:
            identity = str(row.get("id") or "")
            if identity and identity in seen:
                continue
            if identity:
                seen.add(identity)
            deduped.append(row)
        return deduped, bool(
            value.get("has_more") or value.get("truncated")
        )
    if not isinstance(value, list):
        return [], False
    return [row for row in value if isinstance(row, dict)], False


def dispatch_background_work_route(
    aggregator: BackgroundWorkAggregator,
    method: str,
    path: str,
    *,
    context: Any,
    query: dict | None = None,
) -> BackgroundWorkHttpResult | None:
    if path != "/v1/background-work":
        return None
    if method != "GET":
        return BackgroundWorkHttpResult({"error": "METHOD_NOT_ALLOWED"}, 405)
    query = query or {}
    try:
        def value(name: str, default: Any = ""):
            raw = query.get(name, default)
            return raw[0] if isinstance(raw, list) else raw
        body = aggregator.snapshot(context, project=str(value("project", "")), limit=int(value("limit", 100)))
        return BackgroundWorkHttpResult(body)
    except PermissionError as exc:
        return BackgroundWorkHttpResult({"error": "FORBIDDEN", "message": str(exc)}, 403)
    except (TypeError, ValueError) as exc:
        return BackgroundWorkHttpResult({"error": "INVALID_INPUT", "message": str(exc)}, 400)


def handle_background_work_request(
    host: Any,
    method: str,
    path: str,
    *,
    context: Any,
    query: dict | None = None,
) -> BackgroundWorkHttpResult | None:
    """Small host adapter for serving code with an injected aggregator.

    Composition owns construction and authentication.  A host only needs to
    expose ``_background_work_aggregator`` (or the public equivalent) and can
    then add a single dispatch call without importing persistence or ``server``.
    """
    aggregator = getattr(host, "_background_work_aggregator", None)
    if aggregator is None:
        aggregator = getattr(host, "background_work_aggregator", None)
    if not isinstance(aggregator, BackgroundWorkAggregator):
        return None
    return dispatch_background_work_route(
        aggregator, method, path, context=context, query=query,
    )
