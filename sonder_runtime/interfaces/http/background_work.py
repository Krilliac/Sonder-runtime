"""Read-only, authenticated aggregation for the Agents surface.

The serving host supplies the three already-scoped status providers.  Keeping
the aggregation here means the HTTP boundary does not need to import the
legacy ``server`` module (or any persistence adapter), and makes the response
safe to exercise with small in-memory providers.
"""
from __future__ import annotations

import json
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

PREVIEW_CHARS = 240
MAX_ID_CHARS = 256
MAX_STATUS_CHARS = 64
MAX_PROJECT_CHARS = 2_048
MAX_PHASE_CHARS = 128
MAX_LIMIT = 200
MAX_FLEET_CHILDREN = 256
MAX_PLAN_JSON_BYTES = 262_144
MAX_PLAN_TASKS = 500
MAX_ERROR_CHARS = 240


@dataclass(frozen=True)
class BackgroundWorkHttpResult:
    body: dict[str, Any]
    status_code: int = 200


def _text(value: Any, limit: int = PREVIEW_CHARS) -> str:
    return str(value or "")[:limit]


def _finite_number(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if math.isfinite(parsed) else default


def _stamp(row: dict[str, Any], *names: str) -> float:
    for name in names:
        raw = row.get(name)
        if raw is None or isinstance(raw, bool):
            continue
        value = _finite_number(raw, 0.0)
        if value > 0:
            return value
    return 0.0


def _integer(value: Any, default: int = 0) -> int:
    """Read bounded numeric metadata without letting a corrupt row abort a page."""
    try:
        if isinstance(value, bool):
            return default
        if isinstance(value, float) and not math.isfinite(value):
            return default
        if value is None or value == "":
            return 0
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return default


def _elapsed(row: dict[str, Any], now: float) -> float:
    started = _stamp(row, "started_ts", "created_ts", "created_at")
    ended = _stamp(row, "finished_ts", "completed_ts")
    if not ended and str(row.get("status") or "") in {
        "done", "completed", "failed", "cancelled", "interrupted",
    }:
        ended = _stamp(row, "updated_ts", "updated_at")
    now_safe = _finite_number(now, 0.0)
    return round(max(0.0, (ended or now_safe) - started), 3) if started else 0.0


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
    status = _text(row.get("status") or "unknown", MAX_STATUS_CHARS)
    return {
        "id": _text(row.get("id") or row.get("lane_id") or "", MAX_ID_CHARS),
        "task": _text(row.get("task"), 2_000),
        "status": status,
        "project": _text(row.get("project"), MAX_PROJECT_CHARS),
        "updated_ts": _stamp(row, "updated_ts", "updated_at", "created_ts"),
        "created_order": _integer(row.get("created_order")),
        "elapsed_seconds": _elapsed(row, now),
        "preview": _preview(row),
        "cancelable": status not in {
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
    status = _text(row.get("status") or "unknown", MAX_STATUS_CHARS)
    return {
        "id": _text(row.get("id"), MAX_ID_CHARS),
        "task": _text(row.get("task"), 2_000),
        "status": status,
        "activity": _text(row.get("activity")),
        "updated_ts": _stamp(row, "updated_ts", "started_ts"),
        "elapsed_seconds": _elapsed(row, now),
        "preview": _preview(row),
        "cancelable": status in {"queued", "running"},
    }


def _fleet(master: dict[str, Any], children: list[dict[str, Any]], now: float) -> dict[str, Any]:
    counts = {"done": 0, "running": 0, "queued": 0, "failed": 0,
              "cancelled": 0, "other": 0}
    sorted_children = sorted(
        children,
        key=lambda item: _stamp(item, "updated_ts", "started_ts"),
        reverse=True,
    )
    for row in sorted_children:
        counts[_status_bucket(row.get("status"))] += 1
    public_children = [_child(row, now) for row in sorted_children[:MAX_FLEET_CHILDREN]]
    children_truncated = bool(master.get("children_truncated")) or (
        len(sorted_children) > MAX_FLEET_CHILDREN
    )
    if isinstance(master.get("child_counts"), dict):
        counts = dict.fromkeys(counts, 0)
        for status, count in master["child_counts"].items():
            counts[_status_bucket(status)] += _integer(count)
    requested = _integer(master.get("requested_agents"), len(children))
    slots = _integer(master.get("worker_slots"))
    status = _text(master.get("status") or "unknown", MAX_STATUS_CHARS)
    return {
        "id": _text(master.get("id"), MAX_ID_CHARS),
        "task": _text(master.get("task"), 2_000),
        "status": status,
        "project": _text(master.get("project"), MAX_PROJECT_CHARS),
        "requested_agents": requested,
        "worker_slots": slots,
        "updated_ts": _stamp(master, "updated_ts", "started_ts", "created_ts"),
        "elapsed_seconds": _elapsed(master, now),
        "counts": counts,
        "children": public_children,
        "children_truncated": children_truncated,
        "created_ts": _stamp(master, "started_ts", "created_ts", "updated_ts"),
        "preview": _preview(master),
        "cancelable": status in {"queued", "running"},
    }


def _autopilot(row: dict[str, Any], now: float) -> dict[str, Any]:
    plan = row.get("plan")
    if plan is None and row.get("plan_json"):
        raw_plan = row["plan_json"]
        if isinstance(raw_plan, str) and len(raw_plan.encode("utf-8")) <= MAX_PLAN_JSON_BYTES:
            try:
                plan = json.loads(raw_plan)
            except (TypeError, ValueError):
                plan = []
        else:
            plan = []
    if not isinstance(plan, list):
        plan = []
    bounded_plan = plan[:MAX_PLAN_TASKS]
    counts = {"total": len(plan), "done": 0, "running": 0, "queued": 0,
              "failed": 0, "cancelled": 0, "other": 0}
    for task in bounded_plan:
        status = task.get("status") if isinstance(task, dict) else "other"
        counts[_status_bucket(status)] += 1
    current = row.get("current_task")
    current_task = ""
    if (
        isinstance(current, int)
        and not isinstance(current, bool)
        and 0 <= current < len(bounded_plan)
        and isinstance(bounded_plan[current], dict)
    ):
        current_task = _text(
            bounded_plan[current].get("title") or bounded_plan[current].get("task"), 2_000
        )
    elif isinstance(current, dict):
        current_task = _text(current.get("title") or current.get("task"), 2_000)
    status = _text(row.get("status") or "unknown", MAX_STATUS_CHARS)
    return {
        "id": _text(row.get("id"), MAX_ID_CHARS),
        "objective": _text(row.get("objective"), 2_000),
        "status": status,
        "phase": _text(row.get("phase"), MAX_PHASE_CHARS),
        "current_task": current_task,
        "task_counts": counts,
        "updated_ts": _stamp(row, "updated_ts", "created_ts"),
        "created_ts": _stamp(row, "created_ts", "started_ts", "updated_ts"),
        "elapsed_seconds": _elapsed(row, now),
        "preview": _preview(row),
        "cancelable": status not in {"completed", "failed", "cancelled"},
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
        if isinstance(limit, bool) or (isinstance(limit, float) and not math.isfinite(limit)):
            raise ValueError("limit must be a positive integer")
        try:
            raw_limit = int(limit or 100)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("limit must be a positive integer") from exc
        cap = max(1, min(raw_limit, MAX_LIMIT))
        if not isinstance(project, str) or len(project) > MAX_PROJECT_CHARS or "\x00" in project:
            raise ValueError("project must be bounded text")
        raw_now = self._clock()
        now = (
            float(raw_now)
            if isinstance(raw_now, (int, float))
            and not isinstance(raw_now, bool)
            and math.isfinite(raw_now)
            else time.time()
        )
        lane_result = self._lanes(context, limit=cap)
        lane_rows, lane_truncated = _rows_and_truncation(lane_result, "lanes")
        # Keep the provider call within the caller's bounded page.  A status
        # facade may additionally return ``masters`` alongside its child page;
        # the response says when that provider is incomplete.
        fleet_result = self._fleets(owner_id=owner, project=project, limit=cap)
        fleet_rows, fleet_truncated = _rows_and_truncation(fleet_result, "agents")
        auto_result = self._autopilot(request_owner=owner, limit=cap)
        auto_rows, auto_truncated = _rows_and_truncation(auto_result, "runs")
        masters: list[dict[str, Any]] = []
        children_by_parent: dict[str, list[dict[str, Any]]] = {}
        for row in fleet_rows:
            parent_id = str(row.get("parent_id") or "")
            if str(row.get("role") or "") == "master" and not parent_id:
                masters.append(row)
            elif parent_id:
                children_by_parent.setdefault(parent_id, []).append(row)
        fleets = [
            _fleet(master, children_by_parent.get(str(master.get("id") or ""), []), now)
            for master in masters
        ]
        sorted_lanes = _sort_lanes(lane_rows, now)
        if len(sorted_lanes) > cap:
            sorted_lanes = sorted_lanes[:cap]
            lane_truncated = True
        sorted_fleets = sorted(fleets, key=lambda row: row["created_ts"], reverse=True)
        if len(sorted_fleets) > cap:
            sorted_fleets = sorted_fleets[:cap]
            fleet_truncated = True
        sorted_autopilot = sorted(
            (_autopilot(row, now) for row in auto_rows),
            key=lambda row: row["created_ts"],
            reverse=True,
        )
        if len(sorted_autopilot) > cap:
            sorted_autopilot = sorted_autopilot[:cap]
            auto_truncated = True
        groups = {
            "lanes": sorted_lanes,
            "fleets": sorted_fleets,
            "autopilot": sorted_autopilot,
        }
        return {
            "captured_at": now,
            "groups": groups,
            "truncated": {
                "lanes": lane_truncated,
                "fleets": fleet_truncated or any(row["children_truncated"] for row in sorted_fleets),
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
    owner = request_owner(auth)

    def lane_rows(context, *, limit=100):
        return application.agent_lanes().list(context, limit=limit, newest_first=True)

    def fleet_rows(*, owner_id="", project="", limit=100):
        # Legacy fleet ownership is a worker-process identity, not an HTTP
        # account. Only the existing administrator boundary may list it.
        return fleet_snapshot(limit=limit, project=project) if is_admin else []

    def autopilot_rows(*, request_owner="", limit=100):
        return autopilot_snapshot(limit=limit, request_owner=owner)

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
    if query is None:
        params: Mapping[str, Any] = {}
    elif isinstance(query, Mapping):
        params = query
    else:
        return BackgroundWorkHttpResult(
            {"error": "INVALID_INPUT", "message": "query must be a mapping"}, 400
        )
    try:
        def value(name: str, default: Any = "") -> Any:
            raw = params.get(name, default)
            if isinstance(raw, (list, tuple)):
                return raw[0] if raw else default
            return raw

        raw_project = value("project", "")
        if raw_project is None:
            project = ""
        elif isinstance(raw_project, str):
            project = raw_project
        else:
            raise ValueError("project must be a string")

        raw_limit = value("limit", 100)
        if raw_limit is None or raw_limit == "":
            parsed_limit = 100
        elif isinstance(raw_limit, bool):
            raise ValueError("limit must be a positive integer")
        elif isinstance(raw_limit, int):
            parsed_limit = raw_limit
        elif isinstance(raw_limit, str):
            stripped = raw_limit.strip()
            if not stripped:
                parsed_limit = 100
            else:
                parsed_limit = int(stripped, 10)
        else:
            raise ValueError("limit must be a positive integer")
        if parsed_limit <= 0:
            raise ValueError("limit must be a positive integer")

        body = aggregator.snapshot(context, project=project, limit=parsed_limit)
        return BackgroundWorkHttpResult(body)
    except PermissionError as exc:
        return BackgroundWorkHttpResult(
            {"error": "FORBIDDEN", "message": _text(exc, MAX_ERROR_CHARS)}, 403
        )
    except (TypeError, ValueError, OverflowError) as exc:
        return BackgroundWorkHttpResult(
            {"error": "INVALID_INPUT", "message": _text(exc, MAX_ERROR_CHARS)}, 400
        )


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
