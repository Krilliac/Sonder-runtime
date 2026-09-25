"""Typed ``ToolExecutor`` for the developer tools, over ``DeveloperToolServices``.

``tool_inventory``, ``test_run``, ``test_run_result`` and ``output_digest``
are served here; every other tool goes to the fallback executor. Results are
compact JSON of at most 48,000 UTF-8 bytes. Failures are typed results with a
stable ``error_code``; nothing here returns an ``ERROR:`` string.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Mapping

from ..application.context import OperationContext
from ..application.ports.tool_execution import ToolExecutionResult
from ..application.ports.tool_registry import ToolCall, ToolDescriptor
from ..application.testing.ports import TestRunRequest
from ..application.testing.service import MAX_RESULT_WAIT_SECONDS, MAX_RUN_WAIT_SECONDS
from ..domain.common.errors import (
    CapacityExceeded,
    DependencyUnavailable,
    InvalidInput,
    NotFound,
    SonderError,
)
from ..domain.testing.report import MAX_WIRE_BYTES, TestReport, fit_wire
from ..domain.tools.descriptors import ExecutionClass

logger = logging.getLogger(__name__)

DEVELOPER_TYPED_TOOLS = ("tool_inventory", "test_run", "test_run_result", "output_digest")

KNOWN_ERROR_CODES = frozenset({
    "INVALID_SELECTOR", "SELECTOR_UNSUPPORTED", "SELECTOR_ESCAPES_PROJECT",
    "WORKERS_UNSUPPORTED", "NO_RUNNER_DETECTED", "RUNNER_UNAVAILABLE",
    "CTEST_BUILD_TREE_MISSING", "PROJECT_OUTSIDE_ROOTS", "TEST_RUN_BUSY", "JOB_NOT_FOUND",
    "INVALID_INVENTORY_QUERY", "DIGEST_SOURCE_REJECTED", "INVALID_RUNNER",
    "batch_argument_unsafe",
})
DEVELOPER_TOOLS_UNAVAILABLE = "DEVELOPER_TOOLS_UNAVAILABLE"
DEFAULT_RUN_WAIT_SECONDS = 60


def _dumps(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def _size(payload: Mapping[str, Any]) -> int:
    return len(_dumps(payload).encode("utf-8"))


def _fit_mapping(payload: dict, list_keys: tuple[str, ...], max_bytes: int = MAX_WIRE_BYTES) -> dict:
    """Halve the named list fields until the payload fits; flag the cut."""
    for key in list_keys:
        while isinstance(payload.get(key), list) and payload[key] and _size(payload) > max_bytes:
            payload[key] = payload[key][: len(payload[key]) // 2]
            payload["truncated"] = True
    if _size(payload) > max_bytes:
        payload = {k: v for k, v in payload.items() if not isinstance(v, (list, dict))}
        payload["truncated"] = True
    return payload


def _int_argument(arguments: Mapping[str, Any], name: str, default: int | None,
                  low: int, high: int) -> int | None:
    value = arguments.get(name, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidInput("%s must be an integer" % name)
    return max(low, min(high, value))


def test_run_request(arguments: Mapping[str, Any]) -> TestRunRequest:
    """The one mapping from ``test_run`` arguments to a request.

    Shared by the executor and the permission evaluator, so the command an
    operator approves is planned from exactly the request that then runs.
    """
    return TestRunRequest(
        project=str(arguments.get("project") or "."),
        runner=str(arguments.get("runner") or "auto"),
        selector=str(arguments.get("selector") or ""),
        timeout_seconds=_int_argument(arguments, "timeout_seconds", None, 10, 1800),
        workers=_int_argument(arguments, "workers", None, 1, 8),
    )


test_run_request.__test__ = False  # not a pytest test function


class DeveloperToolExecutor:
    """Serve the developer tools; delegate everything else to ``fallback``."""

    NAMES = frozenset(DEVELOPER_TYPED_TOOLS)

    def __init__(self, services, fallback, *,
                 inventory_wire: Callable[[Any], Mapping[str, Any]] | None = None) -> None:
        self._services = services
        self._fallback = fallback
        self._inventory_wire = inventory_wire

    def execute(self, descriptor: ToolDescriptor, call: ToolCall, context: OperationContext,
                execution_class: ExecutionClass) -> ToolExecutionResult:
        name = descriptor.name
        if name not in self.NAMES:
            return self._fallback.execute(descriptor, call, context, execution_class)
        started = time.monotonic()
        if self._services is None:
            return self._failure(name, DEVELOPER_TOOLS_UNAVAILABLE,
                                 "developer tools are not composed in this runtime", started)
        arguments = dict(call.arguments)
        try:
            if name == "tool_inventory":
                payload = self._tool_inventory(arguments)
            elif name == "test_run":
                payload = self._test_run(arguments, context)
            elif name == "test_run_result":
                payload = self._test_run_result(arguments, context)
            else:
                payload = self._output_digest(arguments, context)
        except NotFound as exc:
            return self._failure(name, "JOB_NOT_FOUND" if name != "output_digest" or arguments.get("job_id")
                                 else "DIGEST_SOURCE_REJECTED", str(exc), started)
        except CapacityExceeded as exc:
            return self._failure(name, self._code(exc, "TEST_RUN_BUSY"), str(exc), started)
        except InvalidInput as exc:
            fallback = "INVALID_INVENTORY_QUERY" if name == "tool_inventory" else (
                "DIGEST_SOURCE_REJECTED" if name == "output_digest" else "INVALID_INPUT")
            return self._failure(name, self._code(exc, fallback), str(exc), started)
        except PermissionError as exc:
            code = {"test_run": "RUNNER_UNAVAILABLE", "output_digest": "DIGEST_SOURCE_REJECTED"}.get(
                name, "FORBIDDEN")
            return self._failure(name, code, str(exc) or "refused", started)
        except ImportError:
            return self._failure(name, DEVELOPER_TOOLS_UNAVAILABLE,
                                 "developer tools are not composed in this runtime", started)
        except DependencyUnavailable as exc:
            return self._failure(name, "DEPENDENCY_UNAVAILABLE", str(exc), started)
        except SonderError as exc:
            return self._failure(name, getattr(exc, "code", "INTERNAL_FAILURE"), str(exc), started)
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError) as exc:
            code = "DIGEST_SOURCE_REJECTED" if name == "output_digest" else "INVALID_INPUT"
            return self._failure(name, code, type(exc).__name__, started)
        output = _dumps(payload)
        return ToolExecutionResult(
            tool_name=name, success=True, output=output,
            duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            metadata={"evidence": self._evidence(name, payload)},
        )

    # -- handlers --------------------------------------------------------------

    def _tool_inventory(self, arguments: Mapping[str, Any]) -> dict:
        category = arguments.get("category") or None
        name = arguments.get("name") or None
        refresh = arguments.get("refresh", False)
        if not isinstance(refresh, bool):
            raise InvalidInput("refresh must be a boolean")
        view = self._services.inventory.view(category=category, name=name, refresh=refresh,
                                             redacted=True)
        wire = self._inventory_wire
        if wire is None:
            from ..domain.host_tools.model import view_to_wire as wire  # lane A domain
        payload = dict(wire(view))
        payload.setdefault("ok", True)
        return _fit_mapping(payload, ("tools",))

    def _test_run(self, arguments: Mapping[str, Any], context: OperationContext) -> dict:
        request = test_run_request(arguments)
        wait = _int_argument(arguments, "wait_seconds", DEFAULT_RUN_WAIT_SECONDS, 0,
                             MAX_RUN_WAIT_SECONDS)
        return self._result_payload(self._services.test_runs.run(request, context, wait_seconds=wait))

    def _test_run_result(self, arguments: Mapping[str, Any], context: OperationContext) -> dict:
        job_id = arguments.get("job_id")
        if not isinstance(job_id, str):
            raise NotFound("test run not found")
        wait = _int_argument(arguments, "wait_seconds", 0, 0, MAX_RESULT_WAIT_SECONDS)
        return self._result_payload(self._services.test_runs.result(job_id, context, wait_seconds=wait))

    def _output_digest(self, arguments: Mapping[str, Any], context: OperationContext) -> dict:
        job_id = arguments.get("job_id") or ""
        path = arguments.get("path") or ""
        if bool(job_id) == bool(path):
            raise InvalidInput("give exactly one of job_id or path")
        tail = _int_argument(arguments, "tail_lines", 20, 1, 200)
        failures = _int_argument(arguments, "max_failure_lines", 40, 1, 200)
        digest = self._services.digest
        if job_id:
            result = digest.digest_job(str(job_id), context, tail_lines=tail,
                                       max_failure_lines=failures, operator=False)
        else:
            result = digest.digest_file(str(path), context, tail_lines=tail,
                                        max_failure_lines=failures)
        payload = dict(result.to_wire() if hasattr(result, "to_wire") else result)
        payload.setdefault("ok", True)
        return _fit_mapping(payload, ("tail", "groups", "first_errors", "failure_lines"))

    # -- helpers ---------------------------------------------------------------

    @staticmethod
    def _result_payload(result) -> dict:
        if isinstance(result, TestReport):
            payload = fit_wire(result)
        else:
            payload = dict(result.to_wire())
        payload["ok"] = True
        return payload

    @staticmethod
    def _code(exc: Exception, default: str) -> str:
        code = getattr(exc, "code", "")
        return code if code in KNOWN_ERROR_CODES else default

    @staticmethod
    def _evidence(name: str, payload: Mapping[str, Any]) -> dict:
        evidence = {"tool": name}
        for key in ("job_id", "command_digest", "status", "snapshot_digest"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                evidence[key] = value
        return evidence

    @staticmethod
    def _failure(name: str, code: str, message: str, started: float) -> ToolExecutionResult:
        body = {"ok": False, "error_code": code, "message": str(message)[:400]}
        return ToolExecutionResult(
            tool_name=name, success=False, output=_dumps(body), error_code=code,
            error=str(message)[:400],
            duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            metadata={"evidence": {"tool": name, "error_code": code}},
        )


__all__ = [
    "DEVELOPER_TOOLS_UNAVAILABLE", "DEVELOPER_TYPED_TOOLS", "DeveloperToolExecutor",
    "test_run_request",
]
