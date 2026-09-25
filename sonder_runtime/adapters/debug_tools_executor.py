"""Typed ``ToolExecutor`` for the crash and profile digest tools.

``crash_triage``, ``crash_digest``, ``profile_digest``,
``profile_capture_digest`` and ``debug_run_result`` are served here over
``DebugDigestService``; every other tool goes to the fallback executor (the
developer tools, then the packaged executor). Results are compact JSON of at
most 48,000 UTF-8 bytes; failures are typed results with a stable
``error_code`` (``{"ok": false, "error_code": ...}``), never ``ERROR:``
strings -- the same envelope as ``DeveloperToolExecutor``.

A model call can never set ``console_confirmed``: ``symbol_server=true``
through this executor is always ``SYMBOL_SERVER_NEEDS_CONSOLE``.

Everything returned here is model-visible, so host paths in it are redacted
on the way out: module paths, source files, labels, notes and messages go
through the -2 ``display_redactor`` (workspace roots, the home directory and
the user name) and home directories of other users named by the capture
(``/home/<name>``, ``/Users/<name>``, ``C:\\Users\\<name>``) lose the name.
The service keeps the unredacted report for the console and the crash-fix
hand-off; only this wire copy is redacted.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Callable, Mapping

from ..application.context import OperationContext
from ..application.debugging.ports import (
    CRASH_ENGINES,
    DEBUG_TOOLS_UNAVAILABLE,
    ERROR_CODES,
    PROFILE_ENGINES,
    CrashDigestRequest,
    CrashTriageRequest,
    ProfileDigestRequest,
)
from ..application.ports.tool_execution import ToolExecutionResult
from ..application.ports.tool_registry import ToolCall, ToolDescriptor
from ..domain.common.errors import (
    CapacityExceeded,
    DependencyUnavailable,
    Forbidden,
    InvalidInput,
    NotFound,
    SonderError,
)
from ..domain.tools.descriptors import ExecutionClass

logger = logging.getLogger(__name__)

DEBUG_TYPED_TOOLS = ("crash_triage", "crash_digest", "profile_digest", "profile_capture_digest",
                     "debug_run_result")
MAX_WIRE_BYTES = 48_000
DEFAULT_RUN_WAIT_SECONDS = 60
UNAVAILABLE_MESSAGE = "debug tools are not composed in this runtime"

# A path token starts a string or follows a separator, so relative source
# paths such as ``Engine/Render/x.cpp`` are never cut in half.
_PATH_TOKEN = re.compile(r"(?<![\w.~/\\-])(?:[A-Za-z]:[\\/]|\\\\|/)[^\s'\"<>|,;()\[\]{}]*")
_FOREIGN_HOME = re.compile(r"^(/(?:home|Users)/)([^/]+)(?=/|$)")
_FOREIGN_WINDOWS_HOME = re.compile(r"^([A-Za-z]:[\\/])Users[\\/][^\\/]+(?=$|[\\/])", re.IGNORECASE)
# Identity and enum fields are never paths; leave them byte-exact.
_NOT_PATHS = frozenset({
    "object", "run_id", "command_digest", "input_sha256", "signature", "signature_basis",
    "status", "error_code", "debug_id", "engines", "staging", "egress_isolation", "symbols",
    "trust", "symbolication", "source_kind", "next",
})


def default_path_redactor() -> Callable[[str], str]:
    """The -2 display redactor for this host (identity when it cannot be built)."""
    try:
        from .host_tools.guards import display_redactor

        return display_redactor()
    except Exception:  # pragma: no cover - the guard module ships with the runtime
        logger.warning("display redactor unavailable; debug wire paths are not redacted",
                       exc_info=True)
        return lambda text: text


def redact_paths(text: str, path_redactor: Callable[[str], str]) -> str:
    """Redact every path token in ``text`` (host identity and foreign home dirs)."""
    if not isinstance(text, str) or not text or ("/" not in text and "\\" not in text):
        return text

    def one(match: re.Match) -> str:
        token = match.group(0)
        try:
            redacted = path_redactor(token)
        except Exception:
            redacted = token
        if redacted == token:
            # Home directories of other users (the machine that crashed).
            redacted = _FOREIGN_HOME.sub(r"\1<user>", token)
            redacted = _FOREIGN_WINDOWS_HOME.sub(r"\1Users\\<user>", redacted)
        return redacted

    return _PATH_TOKEN.sub(one, text)


def redact_wire(value: Any, path_redactor: Callable[[str], str], key: str = "") -> Any:
    """``value`` with every path-bearing string redacted (dicts and lists walked)."""
    if isinstance(value, str):
        return value if key in _NOT_PATHS else redact_paths(value, path_redactor)
    if isinstance(value, Mapping):
        return {name: redact_wire(item, path_redactor, str(name)) for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_wire(item, path_redactor, key) for item in value]
    return value


def _dumps(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def _size(payload: Mapping[str, Any]) -> int:
    return len(_dumps(payload).encode("utf-8"))


def _int(arguments: Mapping[str, Any], name: str, default: int | None, low: int,
         high: int) -> int | None:
    value = arguments.get(name, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidInput("%s must be an integer" % name)
    return max(low, min(high, value))


def _number(arguments: Mapping[str, Any], name: str, low: float, high: float) -> float | None:
    value = arguments.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidInput("%s must be a number" % name)
    return max(low, min(high, float(value)))


def _string(arguments: Mapping[str, Any], name: str, limit: int) -> str:
    value = arguments.get(name, "")
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > limit or "\x00" in value:
        raise InvalidInput("%s must be a string of at most %d characters" % (name, limit))
    return value


def _path(arguments: Mapping[str, Any]) -> str:
    value = _string(arguments, "path", 1024)
    if not value.strip():
        raise InvalidInput("path is required")
    return value


def _dirs(arguments: Mapping[str, Any]) -> tuple[str, ...]:
    value = arguments.get("symbol_dirs") or ()
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise InvalidInput("symbol_dirs must be a list of paths")
    if len(value) > 8:
        raise InvalidInput("at most 8 symbol_dirs")
    out = []
    for item in value:
        if not isinstance(item, str) or not item or len(item) > 1024:
            raise InvalidInput("symbol_dirs entries must be paths of at most 1024 characters")
        out.append(item)
    return tuple(out)


def crash_digest_request(arguments: Mapping[str, Any]) -> CrashDigestRequest:
    """The one mapping from ``crash_digest`` arguments to a request (executor and evaluator)."""
    engine = str(arguments.get("engine") or "auto")
    if engine not in CRASH_ENGINES:
        raise InvalidInput("engine must be one of %s" % ", ".join(CRASH_ENGINES))
    symbol_server = arguments.get("symbol_server", False)
    if not isinstance(symbol_server, bool):
        raise InvalidInput("symbol_server must be a boolean")
    return CrashDigestRequest(
        path=_path(arguments), executable=_string(arguments, "executable", 1024),
        symbol_dirs=_dirs(arguments), engine=engine, symbol_server=symbol_server,
        timeout_seconds=_int(arguments, "timeout_seconds", None, 10, 900),
    )


def profile_request(arguments: Mapping[str, Any], *, capture: bool) -> ProfileDigestRequest:
    """``profile_digest`` (pure) or ``profile_capture_digest`` (host) arguments."""
    engine = str(arguments.get("engine") or "auto") if capture else "auto"
    if engine not in PROFILE_ENGINES:
        raise InvalidInput("engine must be one of %s" % ", ".join(PROFILE_ENGINES))
    return ProfileDigestRequest(
        path=_path(arguments),
        executable=_string(arguments, "executable", 1024) if capture else "",
        engine=engine, symbol_dirs=_dirs(arguments) if capture else (),
        top_n=_int(arguments, "top_n", 25, 5, 50) or 25,
        frame_budget_ms=_number(arguments, "frame_budget_ms", 1.0, 1000.0),
        thread=_string(arguments, "thread", 64), frame_zone=_string(arguments, "frame_zone", 64),
        timeout_seconds=_int(arguments, "timeout_seconds", None, 10, 900) if capture else None,
    )


class DebugToolExecutor:
    """Serve the debug tools; delegate everything else to ``fallback``."""

    NAMES = frozenset(DEBUG_TYPED_TOOLS)

    def __init__(self, service, fallback, *,
                 path_redactor: Callable[[], Callable[[str], str]] | None = None) -> None:
        self._service = service
        self._fallback = fallback
        # A factory: the home directory, user and file roots are read per call.
        self._path_redactor = path_redactor or default_path_redactor

    def execute(self, descriptor: ToolDescriptor, call: ToolCall, context: OperationContext,
                execution_class: ExecutionClass) -> ToolExecutionResult:
        name = descriptor.name
        if name not in self.NAMES:
            return self._fallback.execute(descriptor, call, context, execution_class)
        started = time.monotonic()
        if self._service is None:
            return self._failure(name, DEBUG_TOOLS_UNAVAILABLE, UNAVAILABLE_MESSAGE, started)
        arguments = dict(call.arguments)
        try:
            payload = getattr(self, "_" + name)(arguments, context)
        except SonderError as exc:
            return self._failure(name, self._code(exc), str(exc), started)
        except ImportError:
            return self._failure(name, DEBUG_TOOLS_UNAVAILABLE, UNAVAILABLE_MESSAGE, started)
        except PermissionError as exc:
            # An OS PermissionError names the host path; report the kind only.
            message = type(exc).__name__ if getattr(exc, "filename", None) else (str(exc) or "refused")
            return self._failure(name, "CAPTURE_REJECTED", message, started)
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError) as exc:
            return self._failure(name, "CAPTURE_REJECTED", type(exc).__name__, started)
        except (OSError, RecursionError, MemoryError) as exc:
            # Host I/O failures carry host paths; report the kind only.
            logger.warning("debug tool %s failed on the host", name, exc_info=True)
            return self._failure(name, "HOST_IO_FAILURE", type(exc).__name__, started)
        payload["ok"] = True
        payload = self._fit(redact_wire(payload, self._path_redactor()))
        return ToolExecutionResult(
            tool_name=name, success=True, output=_dumps(payload),
            duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            metadata={"evidence": self._evidence(name, payload)},
        )

    # -- handlers ----------------------------------------------------------------

    def _crash_triage(self, arguments: Mapping[str, Any], context: OperationContext) -> dict:
        from ..application.debugging import presenters

        request = CrashTriageRequest(path=_path(arguments),
                                     max_threads=_int(arguments, "max_threads", 16, 1, 16) or 16)
        result, notes = self._service.triage_detail(request, context)
        if isinstance(result, tuple):
            return {"object": "crash_buckets", "count": len(result),
                    "buckets": presenters.buckets_to_wire(result), "notes": list(notes)}
        payload = {"object": "crash_report"}
        payload.update(presenters.report_to_wire(result, max_bytes=MAX_WIRE_BYTES - 1_000))
        return payload

    def _crash_digest(self, arguments: Mapping[str, Any], context: OperationContext) -> dict:
        from ..application.debugging import presenters

        request = crash_digest_request(arguments)
        wait = _int(arguments, "wait_seconds", DEFAULT_RUN_WAIT_SECONDS, 0, 120)
        # console_confirmed is never set from a tool call.
        outcome = self._service.crash(request, context, wait_seconds=wait)
        return presenters.outcome_to_wire(outcome, max_bytes=MAX_WIRE_BYTES - 1_000)

    def _profile_digest(self, arguments: Mapping[str, Any], context: OperationContext) -> dict:
        from ..application.debugging import presenters

        digest = self._service.profile_pure(profile_request(arguments, capture=False), context)
        payload = {"object": "profile_digest"}
        payload.update(presenters.digest_to_wire(digest, max_bytes=MAX_WIRE_BYTES - 1_000))
        return payload

    def _profile_capture_digest(self, arguments: Mapping[str, Any], context: OperationContext) -> dict:
        from ..application.debugging import presenters

        request = profile_request(arguments, capture=True)
        wait = _int(arguments, "wait_seconds", DEFAULT_RUN_WAIT_SECONDS, 0, 120)
        outcome = self._service.profile(request, context, wait_seconds=wait)
        return presenters.outcome_to_wire(outcome, max_bytes=MAX_WIRE_BYTES - 1_000)

    def _debug_run_result(self, arguments: Mapping[str, Any], context: OperationContext) -> dict:
        from ..application.debugging import presenters

        run_id = arguments.get("run_id")
        if not isinstance(run_id, str) or len(run_id) > 80:
            error = NotFound("debug run not found")
            error.code = "JOB_NOT_FOUND"
            raise error
        wait = _int(arguments, "wait_seconds", 0, 0, 60)
        outcome = self._service.result(run_id, context, wait_seconds=wait)
        return presenters.outcome_to_wire(outcome, max_bytes=MAX_WIRE_BYTES - 1_000)

    # -- helpers -------------------------------------------------------------------

    @staticmethod
    def _fit(payload: dict) -> dict:
        for key in ("notes", "buckets", "display_argvs"):
            while isinstance(payload.get(key), list) and payload[key] and _size(payload) > MAX_WIRE_BYTES:
                payload[key] = payload[key][: len(payload[key]) // 2]
                payload["truncated"] = True
        if _size(payload) > MAX_WIRE_BYTES:
            payload = {key: value for key, value in payload.items()
                       if not isinstance(value, (list, dict))}
            payload["truncated"] = True
        return payload

    @staticmethod
    def _code(exc: SonderError) -> str:
        code = getattr(exc, "code", "")
        if code in ERROR_CODES:
            return code
        if isinstance(exc, NotFound):
            return "JOB_NOT_FOUND"
        if isinstance(exc, CapacityExceeded):
            return "DEBUG_RUN_BUSY"
        if isinstance(exc, Forbidden):
            return "FORBIDDEN"
        if isinstance(exc, DependencyUnavailable):
            return "DEPENDENCY_UNAVAILABLE"
        if isinstance(exc, InvalidInput):
            return "INVALID_INPUT"
        return code or "INTERNAL_FAILURE"

    @staticmethod
    def _evidence(name: str, payload: Mapping[str, Any]) -> dict:
        evidence = {"tool": name}
        for key in ("run_id", "command_digest", "status", "input_sha256", "signature",
                    "egress_isolation"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                evidence[key] = value[:128]
        crash = payload.get("crash")
        if isinstance(crash, Mapping) and isinstance(crash.get("input_sha256"), str):
            evidence.setdefault("input_sha256", crash["input_sha256"][:128])
        return evidence

    def _failure(self, name: str, code: str, message: str, started: float) -> ToolExecutionResult:
        message = redact_paths(str(message), self._path_redactor())
        body = {"ok": False, "error_code": code, "message": message[:400]}
        return ToolExecutionResult(
            tool_name=name, success=False, output=_dumps(body), error_code=code,
            error=str(message)[:400],
            duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            metadata={"evidence": {"tool": name, "error_code": code}},
        )


__all__ = [
    "DEBUG_TYPED_TOOLS", "DebugToolExecutor", "crash_digest_request", "default_path_redactor",
    "profile_request", "redact_paths", "redact_wire",
]
