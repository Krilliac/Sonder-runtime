"""Admin HTTP projection of the crash and profile digest service.

Routes (all admin-only, the same guard as ``/v1/tools/inventory``):

- ``POST /v1/tools/crash-triage``          pure read; a directory gives buckets
- ``POST /v1/tools/crash-digest``          ``symbol_server=true`` is always 403
- ``POST /v1/tools/profile-digest``        pure formats only
- ``POST /v1/tools/profile-capture-digest`` host profiler run
- ``GET  /v1/tools/debug-runs/<run_id>``   owner-checked result
- ``POST /v1/tools/debug-runs/<run_id>/cancel``

Interfaces layer: requests come from ``application.debugging.ports`` and wire
payloads from ``application.debugging.presenters``. Only paths, engine names
and bounded numbers are caller-controlled; argv, environment and symbol
servers are host-owned. Symbol-server egress needs an attended console, so an
HTTP request asking for it is refused here, before the service is reached
(the service refuses it again with the same code).

Every method returns ``(status, payload)`` with a compact JSON payload of at
most 48,000 bytes; failures carry ``{"ok": false, "error_code": ...}``.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable

from sonder_runtime.application.context import OperationContext
from sonder_runtime.application.errors import (
    CapacityExceeded,
    InvalidInput,
    NotFound,
    SonderError,
)


MAX_RESPONSE_BYTES = 48_000
MAX_PATH_CHARS = 1024
MAX_SYMBOL_DIRS = 8
CRASH_ENGINES = frozenset({
    "auto", "pure", "cdb", "gdb", "lldb", "eu_stack", "minidump_stackwalk", "llvm_symbolizer",
})
PROFILE_ENGINES = frozenset({
    "auto", "perf", "heaptrack_print", "tracy_csvexport", "xperf", "wpaexporter",
})
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
ROUTE_PREFIX = "/v1/tools/"
POST_ROUTES = frozenset({
    "/v1/tools/crash-triage", "/v1/tools/crash-digest",
    "/v1/tools/profile-digest", "/v1/tools/profile-capture-digest",
})
_RUN_ROUTE = re.compile(r"^/v1/tools/debug-runs/(?P<run_id>[^/]{1,80})(?P<cancel>/cancel)?$")

_TRIAGE_KEYS = {"path", "max_threads"}
_CRASH_KEYS = {"path", "executable", "symbol_dirs", "engine", "symbol_server",
               "timeout_seconds", "wait_seconds"}
_PROFILE_KEYS = {"path", "top_n", "frame_budget_ms", "thread", "frame_zone"}
_CAPTURE_KEYS = {"path", "executable", "engine", "symbol_dirs", "top_n", "frame_budget_ms",
                 "thread", "timeout_seconds", "wait_seconds"}

_STATUS_BY_CODE = {
    "SYMBOL_SERVER_NEEDS_CONSOLE": 403,
    "SYMBOL_SERVER_CONSENT_REQUIRED": 403,
    "FORBIDDEN": 403,
    "JOB_NOT_FOUND": 404,
    "NOT_FOUND": 404,
    "DEBUG_RUN_BUSY": 429,
    "CAPACITY_EXCEEDED": 429,
    "CAPTURE_TOO_LARGE": 413,
    "ENGINE_UNAVAILABLE": 409,
    "ENGINE_UNSUPPORTED_ON_PLATFORM": 409,
    "ENGINE_REFUSED_MANAGED_DUMP": 409,
    "CAPTURE_NEEDS_HOST_TOOL": 409,
    "INPUT_CHANGED": 409,
    "DEPENDENCY_UNAVAILABLE": 503,
}


class _BadRequest(Exception):
    pass


def route_kind(method: str, route: str) -> str | None:
    """Which debug route this is (``None`` when it is not one of ours)."""
    if not isinstance(route, str) or not route.startswith(ROUTE_PREFIX):
        return None
    if method == "POST" and route in POST_ROUTES:
        return route[len(ROUTE_PREFIX):]
    match = _RUN_ROUTE.match(route)
    if match is None:
        return None
    if method == "GET" and not match.group("cancel"):
        return "debug-run"
    if method == "POST" and match.group("cancel"):
        return "debug-run-cancel"
    return None


def run_id_of(route: str) -> str:
    match = _RUN_ROUTE.match(route or "")
    return match.group("run_id") if match else ""


def _error(status: int, code: str) -> tuple[int, dict]:
    return status, {"ok": False, "error_code": code, "error": {"code": code}}


def _code(exc: BaseException) -> str:
    return str(getattr(exc, "code", "") or type(exc).__name__)


def _from_exception(exc: BaseException) -> tuple[int, dict]:
    code = _code(exc)
    if isinstance(exc, PermissionError):
        return _error(403, "FORBIDDEN")
    if isinstance(exc, NotFound):
        return _error(404, "JOB_NOT_FOUND")
    if isinstance(exc, CapacityExceeded):
        return _error(429, code if code != "CAPACITY_EXCEEDED" else "DEBUG_RUN_BUSY")
    if code in _STATUS_BY_CODE:
        return _error(_STATUS_BY_CODE[code], code)
    if isinstance(exc, InvalidInput):
        return _error(400, code or "INVALID_INPUT")
    return _error(503, code or "DEBUG_TOOLS_UNAVAILABLE")


def _compact(body: dict) -> bytes:
    return json.dumps(body, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def _fit(body: dict) -> tuple[int, dict]:
    if len(_compact(body)) <= MAX_RESPONSE_BYTES:
        return 200, body
    # The presenters already halve lists to fit; notes are the only other
    # unbounded field. Past that, answer with the truncation marker only.
    body = dict(body, notes=list(body.get("notes", ()))[:4], truncated=True)
    if len(_compact(body)) <= MAX_RESPONSE_BYTES:
        return 200, body
    return _error(413, "OUTPUT_LIMIT")


def _string(payload: dict, key: str, *, required: bool = False, limit: int = MAX_PATH_CHARS) -> str:
    value = payload.get(key, "")
    if value is None:
        value = ""
    if not isinstance(value, str) or len(value) > limit or "\x00" in value:
        raise _BadRequest()
    if required and not value.strip():
        raise _BadRequest()
    return value


def _number(payload: dict, key: str, low: int, high: int, default: Any = None) -> Any:
    if key not in payload or payload[key] is None:
        return default
    value = payload[key]
    if type(value) is not int or not low <= value <= high:
        raise _BadRequest()
    return value


def _symbol_dirs(payload: dict) -> tuple[str, ...]:
    value = payload.get("symbol_dirs", [])
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > MAX_SYMBOL_DIRS:
        raise _BadRequest()
    out = []
    for item in value:
        if not isinstance(item, str) or not item or len(item) > MAX_PATH_CHARS or "\x00" in item:
            raise _BadRequest()
        out.append(item)
    return tuple(out)


def _engine(payload: dict, allowed: frozenset[str]) -> str:
    value = payload.get("engine", "auto")
    if not isinstance(value, str) or value not in allowed:
        raise _BadRequest()
    return value


def _check_keys(payload: Any, allowed: set[str]) -> dict:
    if not isinstance(payload, dict) or set(payload) - allowed:
        raise _BadRequest()
    return payload


class DebugToolsHttpFacade:
    """``(status, payload)`` for each debug route; ``service_getter`` is lazy."""

    def __init__(self, service_getter: Callable[[], Any] | None,
                 *, presenters: Any = None, ports: Any = None) -> None:
        self._service_getter = service_getter
        self._presenters_override = presenters
        self._ports_override = ports

    # -- plumbing --------------------------------------------------------------

    def _modules(self) -> tuple[Any, Any] | None:
        presenters, ports = self._presenters_override, self._ports_override
        try:
            if presenters is None:
                from sonder_runtime.application.debugging import presenters
            if ports is None:
                from sonder_runtime.application.debugging import ports
        except ImportError:
            return None
        return presenters, ports

    def _service(self) -> Any:
        getter = self._service_getter
        if getter is None or not callable(getter):
            return None
        try:
            return getter()
        except Exception:
            return None

    def _outcome(self, outcome: Any, presenters: Any) -> tuple[int, dict]:
        status = str(getattr(getattr(outcome, "status", ""), "value",
                             getattr(outcome, "status", "")) or "")
        code = str(getattr(outcome, "error_code", "") or "")
        if status == "refused" and code:
            return _error(_STATUS_BY_CODE.get(code, 409), code)
        body: dict = {
            "ok": status not in ("failed", "refused"),
            "run_id": str(getattr(outcome, "run_id", "") or ""),
            "status": status,
            "notes": [str(note)[:240] for note in tuple(getattr(outcome, "notes", ()) or ())[:16]],
        }
        if code:
            body["error_code"] = code
        crash = getattr(outcome, "crash", None)
        profile = getattr(outcome, "profile", None)
        if crash is not None:
            body["crash"] = presenters.report_to_wire(crash, max_bytes=MAX_RESPONSE_BYTES - 2_000)
        if profile is not None:
            body["profile"] = presenters.digest_to_wire(profile)
        return _fit(body)

    def _guard(self, admin: bool) -> tuple[Any, Any, Any] | tuple[int, dict]:
        if not admin:
            return _error(403, "FORBIDDEN")
        modules = self._modules()
        service = self._service()
        if modules is None or service is None:
            return _error(503, "DEBUG_TOOLS_UNAVAILABLE")
        return service, modules[0], modules[1]

    # -- routes ----------------------------------------------------------------

    def crash_triage(self, payload: Any, context: OperationContext, *, admin: bool) -> tuple[int, dict]:
        guard = self._guard(admin)
        if isinstance(guard[0], int):
            return guard  # type: ignore[return-value]
        service, presenters, ports = guard
        try:
            body = _check_keys(payload, _TRIAGE_KEYS)
            request = ports.CrashTriageRequest(
                path=_string(body, "path", required=True),
                max_threads=_number(body, "max_threads", 1, 16, 16),
            )
        except _BadRequest:
            return _error(400, "INVALID_DEBUG_REQUEST")
        try:
            result = service.triage(request, context)
        except (SonderError, PermissionError) as exc:
            return _from_exception(exc)
        if isinstance(result, tuple):
            buckets = presenters.buckets_to_wire(result) if hasattr(
                presenters, "buckets_to_wire") else [_bucket_wire(item) for item in result]
            return _fit({"ok": True, "buckets": buckets})
        return _fit({"ok": True, "crash": presenters.report_to_wire(
            result, max_bytes=MAX_RESPONSE_BYTES - 2_000)})

    def crash_digest(self, payload: Any, context: OperationContext, *, admin: bool) -> tuple[int, dict]:
        if not admin:
            return _error(403, "FORBIDDEN")
        if isinstance(payload, dict) and payload.get("symbol_server") not in (None, False):
            # Console-only egress: an HTTP caller can never ask for it.
            return _error(403, "SYMBOL_SERVER_NEEDS_CONSOLE")
        guard = self._guard(admin)
        if isinstance(guard[0], int):
            return guard  # type: ignore[return-value]
        service, presenters, ports = guard
        try:
            body = _check_keys(payload, _CRASH_KEYS)
            request = ports.CrashDigestRequest(
                path=_string(body, "path", required=True),
                executable=_string(body, "executable"),
                symbol_dirs=_symbol_dirs(body),
                engine=_engine(body, CRASH_ENGINES),
                symbol_server=False,
                timeout_seconds=_number(body, "timeout_seconds", 10, 900),
            )
            wait = _number(body, "wait_seconds", 0, 120, 0)
        except _BadRequest:
            return _error(400, "INVALID_DEBUG_REQUEST")
        try:
            outcome = service.crash(request, context, wait_seconds=wait, console_confirmed=False)
        except (SonderError, PermissionError) as exc:
            return _from_exception(exc)
        return self._outcome(outcome, presenters)

    def profile_digest(self, payload: Any, context: OperationContext, *, admin: bool) -> tuple[int, dict]:
        guard = self._guard(admin)
        if isinstance(guard[0], int):
            return guard  # type: ignore[return-value]
        service, presenters, ports = guard
        try:
            body = _check_keys(payload, _PROFILE_KEYS)
            request = ports.ProfileDigestRequest(
                path=_string(body, "path", required=True),
                top_n=_number(body, "top_n", 5, 50, 25),
                frame_budget_ms=_number(body, "frame_budget_ms", 1, 1000),
                thread=_string(body, "thread", limit=64),
                frame_zone=_string(body, "frame_zone", limit=64),
            )
        except _BadRequest:
            return _error(400, "INVALID_DEBUG_REQUEST")
        try:
            digest = service.profile_pure(request, context)
        except (SonderError, PermissionError) as exc:
            return _from_exception(exc)
        return _fit({"ok": True, "profile": presenters.digest_to_wire(digest)})

    def profile_capture_digest(self, payload: Any, context: OperationContext, *,
                               admin: bool) -> tuple[int, dict]:
        guard = self._guard(admin)
        if isinstance(guard[0], int):
            return guard  # type: ignore[return-value]
        service, presenters, ports = guard
        try:
            body = _check_keys(payload, _CAPTURE_KEYS)
            request = ports.ProfileDigestRequest(
                path=_string(body, "path", required=True),
                executable=_string(body, "executable"),
                engine=_engine(body, PROFILE_ENGINES),
                symbol_dirs=_symbol_dirs(body),
                top_n=_number(body, "top_n", 5, 50, 25),
                frame_budget_ms=_number(body, "frame_budget_ms", 1, 1000),
                thread=_string(body, "thread", limit=64),
                timeout_seconds=_number(body, "timeout_seconds", 10, 900),
            )
            wait = _number(body, "wait_seconds", 0, 120, 0)
        except _BadRequest:
            return _error(400, "INVALID_DEBUG_REQUEST")
        try:
            outcome = service.profile(request, context, wait_seconds=wait)
        except (SonderError, PermissionError) as exc:
            return _from_exception(exc)
        return self._outcome(outcome, presenters)

    def run_result(self, run_id: str, context: OperationContext, *, admin: bool,
                   wait_seconds: int = 0) -> tuple[int, dict]:
        guard = self._guard(admin)
        if isinstance(guard[0], int):
            return guard  # type: ignore[return-value]
        service, presenters, _ports = guard
        if not isinstance(run_id, str) or not RUN_ID_PATTERN.match(run_id):
            return _error(404, "JOB_NOT_FOUND")
        if type(wait_seconds) is not int or not 0 <= wait_seconds <= 60:
            return _error(400, "INVALID_DEBUG_REQUEST")
        try:
            outcome = service.result(run_id, context, wait_seconds=wait_seconds)
        except (SonderError, PermissionError) as exc:
            return _from_exception(exc)
        return self._outcome(outcome, presenters)

    def run_cancel(self, run_id: str, context: OperationContext, *, admin: bool) -> tuple[int, dict]:
        guard = self._guard(admin)
        if isinstance(guard[0], int):
            return guard  # type: ignore[return-value]
        service, presenters, _ports = guard
        if not isinstance(run_id, str) or not RUN_ID_PATTERN.match(run_id):
            return _error(404, "JOB_NOT_FOUND")
        try:
            outcome = service.cancel(run_id, context)
        except (SonderError, PermissionError) as exc:
            return _from_exception(exc)
        return self._outcome(outcome, presenters)

    def dispatch(self, method: str, route: str, payload: Any, context: OperationContext,
                 *, admin: bool, wait_seconds: int = 0) -> tuple[int, dict]:
        """Route one request; ``(404, ...)`` for a path this facade does not own."""
        kind = route_kind(method, route)
        if kind == "crash-triage":
            return self.crash_triage(payload, context, admin=admin)
        if kind == "crash-digest":
            return self.crash_digest(payload, context, admin=admin)
        if kind == "profile-digest":
            return self.profile_digest(payload, context, admin=admin)
        if kind == "profile-capture-digest":
            return self.profile_capture_digest(payload, context, admin=admin)
        if kind == "debug-run":
            return self.run_result(run_id_of(route), context, admin=admin,
                                   wait_seconds=wait_seconds)
        if kind == "debug-run-cancel":
            return self.run_cancel(run_id_of(route), context, admin=admin)
        return _error(404, "NOT_FOUND")


def _bucket_wire(bucket: Any) -> dict:
    return {
        "signature": str(getattr(bucket, "signature", ""))[:64],
        "basis": str(getattr(bucket, "basis", ""))[:32],
        "count": int(getattr(bucket, "count", 0) or 0),
        "exception_name": str(getattr(bucket, "exception_name", ""))[:240],
        "top_frame": str(getattr(bucket, "top_frame", ""))[:240],
        "sample_labels": [str(label)[:240] for label in
                          tuple(getattr(bucket, "sample_labels", ()) or ())[:5]],
    }


__all__ = [
    "DebugToolsHttpFacade", "MAX_RESPONSE_BYTES", "POST_ROUTES", "route_kind", "run_id_of",
]
