"""Typed ``ToolExecutor`` for the C++ build tools, over ``BuildToolServices``.

``build_model``, ``build_job``, ``build_job_result``, ``build_fix``,
``build_fix_result`` and ``build_fix_restore`` are served here; every other
tool goes to the fallback executor (in the runtime, the developer-tool
executor, which serves ``test_run``, ``tool_inventory`` and
``output_digest`` and falls back to the packaged executor in turn).

Results are compact JSON of at most 48,000 UTF-8 bytes. Failures are typed
results with a stable ``error_code`` from the build error vocabulary; nothing
here returns an ``ERROR:`` string. Host paths never reach the wire: the
services return label-only views, and an ``OSError`` (whose text carries host
paths) is reported by kind only.

The services' request types live in ``application.build.ports`` (build jobs)
and ``application.build.fix_ports`` (the fix loop). They are imported when a
call needs them, so a runtime without the build packages still composes this
executor and answers ``BUILD_TOOLS_UNAVAILABLE`` instead of failing to start.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Callable, Mapping

from ...application.context import OperationContext
from ...application.ports.tool_execution import ToolExecutionResult
from ...application.ports.tool_registry import ToolCall, ToolDescriptor
from ...domain.common.errors import (
    CapacityExceeded,
    Conflict,
    DependencyUnavailable,
    Forbidden,
    InvalidInput,
    NotFound,
    SonderError,
)
from ...domain.tools.descriptors import ExecutionClass

logger = logging.getLogger(__name__)

BUILD_TYPED_TOOLS = (
    "build_model", "build_job", "build_job_result",
    "build_fix", "build_fix_result", "build_fix_restore",
)
BUILD_TOOLS_UNAVAILABLE = "BUILD_TOOLS_UNAVAILABLE"
MAX_WIRE_BYTES = 48_000
DEFAULT_JOB_WAIT_SECONDS = 60
MAX_WAIT_SECONDS = 120
MODEL_DETAILS = ("summary", "targets", "compile_units", "toolchain", "presets")
JOB_ACTIONS = ("configure", "build", "compile_one", "include_trace")
MAX_RESTORE_FILES = 6
MAX_EDITABLE_GLOBS = 16

# The stable build error vocabulary (docs/architecture/CPP-BUILD-FIX.md).
KNOWN_ERROR_CODES = frozenset({
    "BUILD_MODEL_UNAVAILABLE", "BUILD_TREE_MISSING", "BUILD_TREE_REJECTED",
    "UNKNOWN_TARGET", "UNKNOWN_CONFIG", "UNKNOWN_PLATFORM", "UNKNOWN_PRESET", "UNKNOWN_FILE",
    "UTILITY_TARGET_REFUSED", "ACTION_UNSUPPORTED", "RUNNER_UNAVAILABLE",
    "BUILD_DIR_BUSY", "BUILD_BUSY", "PROJECT_OUTSIDE_ROOTS",
    "ENV_CAPTURE_FAILED", "NETWORK_ISOLATION_UNAVAILABLE",
    "JOB_NOT_FOUND", BUILD_TOOLS_UNAVAILABLE,
    "FIX_SCOPE_REJECTED", "RESIDENCY_REFUSED", "RESTORE_CONFLICT",
    "INVALID_INPUT",
})
# Which code a taxonomy error without a build code maps to, per class.
_CLASS_DEFAULTS: tuple[tuple[type[SonderError], str], ...] = (
    (NotFound, "JOB_NOT_FOUND"),
    (CapacityExceeded, "BUILD_BUSY"),
    (Conflict, "BUILD_DIR_BUSY"),
    (Forbidden, "BUILD_TREE_REJECTED"),
    (DependencyUnavailable, "BUILD_MODEL_UNAVAILABLE"),
    (InvalidInput, "INVALID_INPUT"),
)


def _dumps(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def _size(payload: Mapping[str, Any]) -> int:
    return len(_dumps(payload).encode("utf-8"))


def fit_payload(payload: dict, max_bytes: int = MAX_WIRE_BYTES) -> dict:
    """Halve the largest list fields until the payload fits; flag the cut."""
    if _size(payload) <= max_bytes:
        return payload
    payload = dict(payload)
    for _ in range(64):
        lists = sorted(((len(_dumps({k: v})), k) for k, v in payload.items()
                        if isinstance(v, list) and v), reverse=True)
        if not lists or _size(payload) <= max_bytes:
            break
        key = lists[0][1]
        payload[key] = payload[key][: len(payload[key]) // 2]
        payload["truncated"] = True
    if _size(payload) > max_bytes:
        payload = {k: v for k, v in payload.items() if not isinstance(v, (list, dict))}
        payload = {k: (v[:2000] if isinstance(v, str) else v) for k, v in payload.items()}
        payload["truncated"] = True
    return payload


# --- argument mapping (shared by the executor and the permission resolvers) ------------


def _int(arguments: Mapping[str, Any], name: str, default: int | None,
         low: int, high: int) -> int | None:
    value = arguments.get(name, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidInput("%s must be an integer" % name)
    return max(low, min(high, value))


def _bool(arguments: Mapping[str, Any], name: str, default: bool) -> bool:
    value = arguments.get(name, default)
    if not isinstance(value, bool):
        raise InvalidInput("%s must be a boolean" % name)
    return value


def _str(arguments: Mapping[str, Any], name: str, default: str = "", limit: int = 1024) -> str:
    value = arguments.get(name, default)
    if value is None:
        value = default
    if not isinstance(value, str) or "\x00" in value or len(value) > limit:
        raise InvalidInput("%s must be a bounded string" % name)
    return value


# Defence in depth for the model-supplied names (docs/security/BUILD-TOOLS.md
# section 1). The planner still requires every value to be a member of the
# parsed build model; these checks refuse option-, response-file- and
# shell-shaped values at the surface, before any tree is read, because the
# gateway's schema check does not evaluate ``pattern``.
_NAME_FORBIDDEN = frozenset("=;,%\"'`$&|<>^\x00")
_NAME_LEADING = ("-", "/", "@", "\\", "+", "~")
_MSBUILD_SUFFIX = ":Build"
_PATH_LEADING = ("-", "@")
_UNC_PREFIXES = ("\\\\", "//", "\\??\\", "\\\\?\\", "\\\\.\\")
_DRIVE = re.compile(r"^[A-Za-z]:")
BUILD_GENERATORS = (
    "Ninja", "Ninja Multi-Config", "Unix Makefiles", "NMake Makefiles",
    "Visual Studio 17 2022", "Visual Studio 16 2019",
)


def _name(arguments: Mapping[str, Any], name: str, *, limit: int, spaces: bool = False,
          msbuild_suffix: bool = False) -> str:
    """A build-model member name (target, config, platform, preset, profile)."""
    value = _str(arguments, name, limit=limit)
    if not value:
        return value
    body = value
    if msbuild_suffix and body.endswith(_MSBUILD_SUFFIX):
        body = body[: -len(_MSBUILD_SUFFIX)]
    if (not body or body.startswith(_NAME_LEADING) or ":" in body
            or any(ch in _NAME_FORBIDDEN or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in body)
            or (not spaces and any(ch.isspace() for ch in body))
            or (spaces and (body != body.strip() or "  " in body
                            or any(ch.isspace() and ch != " " for ch in body)))):
        raise InvalidInput("%s must name a member of the build model, not an option or a "
                           "command fragment" % name)
    return value


def _path(arguments: Mapping[str, Any], name: str, default: str = "") -> str:
    """A project, build-dir or file path: no option, response-file or UNC shape."""
    value = _str(arguments, name, default)
    if value and (value.startswith(_PATH_LEADING) or value.startswith(_UNC_PREFIXES)
                  or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)):
        raise InvalidInput("%s must be a local project path" % name)
    return value


def _glob(glob: str) -> bool:
    normalized = glob.replace("\\", "/")
    return (bool(glob) and len(glob) <= 128 and "\x00" not in glob
            and not normalized.startswith(("/", "-", "@", "~")) and not _DRIVE.match(normalized)
            and ".." not in normalized.split("/")
            and not any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in glob))


def build_model_request(arguments: Mapping[str, Any]):
    """``build_model`` arguments -> ``application.build.ports.BuildModelRequest``."""
    from ...application.build.ports import BuildModelRequest

    return BuildModelRequest(
        project=_path(arguments, "project", ".") or ".",
        build_dir=_path(arguments, "build_dir"),
        preset=_name(arguments, "preset", limit=128),
        refresh=_bool(arguments, "refresh", False),
    )


def build_job_request(arguments: Mapping[str, Any]):
    """``build_job`` arguments -> ``BuildJobRequest``.

    Shared by the executor and the permission resolver, so the command an
    operator approves is planned from exactly the request that then runs.
    """
    from ...application.build.ports import BuildJobRequest

    action = _str(arguments, "action", "build", limit=32) or "build"
    if action not in JOB_ACTIONS:
        raise InvalidInput("action must be one of %s" % ", ".join(JOB_ACTIONS))
    generator = _str(arguments, "generator", limit=64)
    if generator and generator not in BUILD_GENERATORS:
        raise InvalidInput("generator must be one of %s" % ", ".join(BUILD_GENERATORS))
    return BuildJobRequest(
        project=_path(arguments, "project", ".") or ".",
        build_dir=_path(arguments, "build_dir"),
        action=action,
        target=_name(arguments, "target", limit=128, msbuild_suffix=True),
        config=_name(arguments, "config", limit=64),
        platform=_name(arguments, "platform", limit=64, spaces=True),
        preset=_name(arguments, "preset", limit=128),
        build_preset=_name(arguments, "build_preset", limit=128),
        file=_path(arguments, "file"),
        generator=generator,
        profile=_name(arguments, "profile", limit=64),
        jobs=_int(arguments, "jobs", None, 1, 256),
        timeout_seconds=_int(arguments, "timeout_seconds", None, 30, 86_400),
        allow_network=_bool(arguments, "allow_network", False),
    )


def build_fix_request(arguments: Mapping[str, Any]):
    """``build_fix`` arguments -> ``application.build.fix_ports.BuildFixRequest``."""
    from ...application.build.fix_ports import BuildFixRequest

    globs = arguments.get("editable_globs", ()) or ()
    if not isinstance(globs, (list, tuple)) or len(globs) > MAX_EDITABLE_GLOBS or any(
            not isinstance(glob, str) or not _glob(glob) for glob in globs):
        raise InvalidInput("editable_globs must be at most 16 project-relative globs")
    target = _name(arguments, "target", limit=128, msbuild_suffix=True)
    if not target:
        raise InvalidInput("build_fix needs a target")
    return BuildFixRequest(
        project=_path(arguments, "project", ".") or ".",
        build_dir=_path(arguments, "build_dir"),
        target=target,
        config=_name(arguments, "config", limit=64),
        platform=_name(arguments, "platform", limit=64, spaces=True),
        focus_file=_path(arguments, "focus_file"),
        attempts=_int(arguments, "attempts", 4, 1, 8),
        apply=_bool(arguments, "apply", True),
        revert_after=_bool(arguments, "revert_after", False),
        editable_globs=tuple(globs),
        timeout_seconds=_int(arguments, "timeout_seconds", None, 60, 86_400),
        verify_dependents=_bool(arguments, "verify_dependents", False),
        allow_network=_bool(arguments, "allow_network", False),
    )


def result_to_wire(result: Any) -> dict:
    """The wire form of a status view, a build report or a fix report."""
    if isinstance(result, Mapping):
        return dict(result)
    kind = type(result).__name__
    if kind == "BuildJobReport":
        from ...domain.build.report import build_report_to_wire

        return dict(build_report_to_wire(result, max_bytes=MAX_WIRE_BYTES))
    if kind == "BuildFixReport":
        from ...domain.build.repair import fix_report_to_wire

        return dict(fix_report_to_wire(result, max_bytes=MAX_WIRE_BYTES))
    to_wire = getattr(result, "to_wire", None)
    if callable(to_wire):
        return dict(to_wire())
    raise TypeError("build service returned an unrenderable %s" % kind)


def os_error_text(exc: BaseException) -> str:
    """Text of an error safe for the wire: an OS error names its kind, never its path.

    ``OSError`` raised by the operating system carries the host path it failed
    on (``[Errno 13] Permission denied: '/home/...'``); a guard that raises
    ``PermissionError("project is outside the roots")`` itself has no errno
    and keeps its message.
    """
    if isinstance(exc, OSError) and (exc.errno is not None or exc.filename is not None):
        return type(exc).__name__
    return str(exc) or type(exc).__name__


def error_code_for(exc: BaseException) -> str:
    """The stable build code of a taxonomy error (the class default otherwise)."""
    code = getattr(exc, "code", "")
    if isinstance(code, str) and code in KNOWN_ERROR_CODES:
        return code
    for cls, default in _CLASS_DEFAULTS:
        if isinstance(exc, cls):
            return default
    return "INVALID_INPUT" if isinstance(exc, (ValueError, TypeError)) else "INTERNAL_FAILURE"


class BuildToolExecutor:
    """Serve the build tools; delegate everything else to ``fallback``.

    ``grants`` is the in-process build-fix grant registry the permission
    evaluator records approvals in when ``build_fix`` (or
    ``build_fix_restore``) is allowed. ``build_fix`` claims the plan approved
    for its own request (``context.correlation_id``) and starts exactly that
    plan; the fix service issues the job's grant from the approval and
    revokes it when the job ends, so it lives exactly as long as that job.
    """

    NAMES = frozenset(BUILD_TYPED_TOOLS)

    def __init__(self, services, fallback, *, grants=None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._services = services
        self._fallback = fallback
        self._grants = grants
        self._clock = clock

    def execute(self, descriptor: ToolDescriptor, call: ToolCall, context: OperationContext,
                execution_class: ExecutionClass) -> ToolExecutionResult:
        name = descriptor.name
        if name not in self.NAMES:
            return self._fallback.execute(descriptor, call, context, execution_class)
        started = self._clock()
        services = self._services
        if services is None or (name.startswith("build_fix") and getattr(services, "fix", None) is None):
            what = "the build fix loop is" if services is not None else "build tools are"
            return self._failure(name, BUILD_TOOLS_UNAVAILABLE,
                                 "%s not composed in this runtime" % what, started)
        arguments = dict(call.arguments)
        try:
            handler = getattr(self, "_" + name)
            payload = handler(arguments, context)
        except ImportError:
            logger.warning("build tool %s: a build package is missing", name, exc_info=True)
            return self._failure(name, BUILD_TOOLS_UNAVAILABLE,
                                 "build tools are not composed in this runtime", started)
        except SonderError as exc:
            return self._failure(name, error_code_for(exc), str(exc), started)
        except PermissionError as exc:
            return self._failure(name, "PROJECT_OUTSIDE_ROOTS", os_error_text(exc), started)
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError) as exc:
            return self._failure(name, "BUILD_TREE_MISSING", type(exc).__name__, started)
        except (ValueError, TypeError) as exc:
            return self._failure(name, "INVALID_INPUT", str(exc), started)
        except (OSError, RecursionError) as exc:
            # Host-side I/O failures carry host paths in their text; report the
            # kind only (the log keeps the detail for the operator).
            logger.warning("build tool %s failed on the host", name, exc_info=True)
            return self._failure(name, "HOST_IO_FAILURE", type(exc).__name__, started)
        payload = fit_payload(payload)
        return ToolExecutionResult(
            tool_name=name, success=True, output=_dumps(payload),
            duration_ms=max(0, int((self._clock() - started) * 1000)),
            metadata={"evidence": self._evidence(name, payload)},
        )

    # -- handlers --------------------------------------------------------------

    def _build_model(self, arguments: Mapping[str, Any], context: OperationContext) -> dict:
        detail = _str(arguments, "detail", "summary", limit=32) or "summary"
        if detail not in MODEL_DETAILS:
            raise InvalidInput("detail must be one of %s" % ", ".join(MODEL_DETAILS))
        payload = dict(self._services.model.view(
            build_model_request(arguments), context, detail=detail,
            target=_name(arguments, "target", limit=128, msbuild_suffix=True),
            max_items=_int(arguments, "max_items", 100, 1, 500),
        ))
        payload.setdefault("ok", True)
        return payload

    def _build_job(self, arguments: Mapping[str, Any], context: OperationContext) -> dict:
        wait = _int(arguments, "wait_seconds", DEFAULT_JOB_WAIT_SECONDS, 0, MAX_WAIT_SECONDS)
        result = self._services.jobs.run(build_job_request(arguments), context, wait_seconds=wait)
        return self._ok(result)

    def _build_job_result(self, arguments: Mapping[str, Any], context: OperationContext) -> dict:
        job_id = self._job_id(arguments)
        if _bool(arguments, "cancel", False):
            return self._ok(self._services.jobs.cancel(job_id, context, reason="cancelled by caller"))
        wait = _int(arguments, "wait_seconds", 0, 0, MAX_WAIT_SECONDS)
        return self._ok(self._services.jobs.result(job_id, context, wait_seconds=wait))

    def _build_fix(self, arguments: Mapping[str, Any], context: OperationContext) -> dict:
        request = build_fix_request(arguments)
        wait = _int(arguments, "wait_seconds", 0, 0, MAX_WAIT_SECONDS)
        grants = self._grants
        # The plan approved for exactly this request (and principal), once: the
        # service starts that plan and issues the job's grant from the approval.
        plan = grants.claim(context.correlation_id, context.principal_id) \
            if grants is not None else None
        fix = self._services.fix
        try:
            job_id = fix.start(request, context, plan=plan)
        finally:
            if plan is not None:
                # A start that issued the grant consumed the approval; any other
                # outcome must not leave it for a later call.
                grants.withdraw(plan.plan_digest, context.principal_id)
        granted = grants is not None and plan is not None and grants.granted_job(job_id)
        if wait:
            return self._ok(fix.result(job_id, context, wait_seconds=wait))
        return {"ok": True, "object": "build_fix_status", "job_id": job_id, "status": "running",
                "grant": "bound" if granted else "none",
                "next": "call build_fix_result with this job_id to wait for the report"}

    def _build_fix_result(self, arguments: Mapping[str, Any], context: OperationContext) -> dict:
        job_id = self._job_id(arguments)
        if _bool(arguments, "cancel", False):
            return self._ok(self._services.fix.cancel(job_id, context))
        wait = _int(arguments, "wait_seconds", 0, 0, MAX_WAIT_SECONDS)
        return self._ok(self._services.fix.result(job_id, context, wait_seconds=wait))

    def _build_fix_restore(self, arguments: Mapping[str, Any], context: OperationContext) -> dict:
        job_id = self._job_id(arguments)
        files = arguments.get("files", ()) or ()
        if not isinstance(files, (list, tuple)) or len(files) > MAX_RESTORE_FILES or any(
                not isinstance(item, str) or not item or len(item) > 1024 or "\x00" in item
                for item in files):
            raise InvalidInput("files must be at most 6 project-relative paths")
        from ...application.build.grants import restore_plan_digest

        grants = self._grants
        approved = grants is not None and grants.claim_restore(
            context.correlation_id, context.principal_id, job_id, tuple(files))
        try:
            return self._ok(self._services.fix.restore(job_id, context, files=tuple(files)))
        finally:
            if approved:
                grants.withdraw(restore_plan_digest(job_id, tuple(files)), context.principal_id)

    # -- helpers ---------------------------------------------------------------

    @staticmethod
    def _job_id(arguments: Mapping[str, Any]) -> str:
        job_id = arguments.get("job_id")
        if not isinstance(job_id, str) or not job_id or len(job_id) > 80:
            raise NotFound("build job not found")
        return job_id

    @staticmethod
    def _ok(result: Any) -> dict:
        payload = result_to_wire(result)
        payload["ok"] = True
        return payload

    @staticmethod
    def _evidence(name: str, payload: Mapping[str, Any]) -> dict:
        evidence = {"tool": name}
        for key in ("job_id", "command_digest", "status", "stop_reason", "digest",
                    "world", "network", "isolation_truth", "verification_scope"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                evidence[key] = value[:128]
        return evidence

    def _failure(self, name: str, code: str, message: str, started: float) -> ToolExecutionResult:
        body = {"ok": False, "error_code": code, "message": str(message)[:400]}
        return ToolExecutionResult(
            tool_name=name, success=False, output=_dumps(body), error_code=code,
            error=str(message)[:400],
            duration_ms=max(0, int((self._clock() - started) * 1000)),
            metadata={"evidence": {"tool": name, "error_code": code}},
        )


__all__ = [
    "BUILD_GENERATORS", "BUILD_TOOLS_UNAVAILABLE", "BUILD_TYPED_TOOLS", "BuildToolExecutor", "KNOWN_ERROR_CODES",
    "build_fix_request", "build_job_request", "build_model_request", "error_code_for",
    "fit_payload", "os_error_text", "result_to_wire",
]
