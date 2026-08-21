"""Saved-workflow use cases independent of MCP and legacy storage modules."""
from __future__ import annotations

from collections.abc import Callable
import inspect
import json
from uuid import uuid4

from ..loop.durable_control import DurableLoopControl
from ..ports.tool_executor import ToolResult
from ..ports.specialized_lifecycle import CleanupResult
from ..ports.workflows import LoopRunner, WorkflowRepository


def _failure(message: object, code: str = "INVALID_INPUT") -> ToolResult:
    return ToolResult(ok=False, output=str(message), error_code=code)


def render_workflow_result(result: ToolResult) -> str:
    """Preserve the historical MCP text while status remains explicitly typed."""
    if result.ok or result.evidence.get("legacy_raw_output") is True:
        return result.output
    return "ERROR: %s" % result.output


def _repository_failure(exc: Exception) -> ToolResult:
    code = (
        "STORAGE_ERROR"
        if isinstance(exc, (OSError, json.JSONDecodeError))
        else "INVALID_INPUT"
    )
    return _failure(exc, code)


class WorkflowService:
    def __init__(
        self,
        repository: WorkflowRepository,
        runner: LoopRunner,
        *,
        loop_control: DurableLoopControl | None = None,
    ) -> None:
        self._repository = repository
        self._runner = runner
        self._loop_control = loop_control or DurableLoopControl()

    @property
    def loop_control(self) -> DurableLoopControl:
        """Expose the durable control plane for an owning job/request boundary."""
        return self._loop_control

    def list(self) -> ToolResult:
        try:
            workflows, path = self._repository.ensure()
        except (OSError, ValueError) as exc:
            return _repository_failure(exc)
        return ToolResult(
            ok=True,
            output="workflows: %s\n\n%s" % (
                path, self._repository.format(workflows),
            ),
        )

    def save(
        self, name: str, actions_json: str, description: str = ""
    ) -> ToolResult:
        try:
            parsed = json.loads(actions_json)
        except (json.JSONDecodeError, TypeError) as exc:
            return _failure("actions_json is not valid JSON: %s" % exc)
        actions = parsed.get("actions") if isinstance(parsed, dict) else parsed
        try:
            workflow, path = self._repository.save(name, actions, description)
            normalized = self._repository.normalize_name(name)
        except (OSError, ValueError) as exc:
            return _repository_failure(exc)
        return ToolResult(
            ok=True,
            output="Saved workflow '%s' to %s (%d actions)." % (
                normalized, path, len(workflow["actions"]),
            ),
        )

    def run(
        self,
        name: str,
        dispatch: Callable[[dict], dict],
        *,
        max_iterations: int = 1,
        stop_on_failure: bool = True,
        stop_on_success: bool = False,
        delay_seconds: float = 0,
        cancel_check: Callable[[], bool] | None = None,
    ) -> ToolResult:
        try:
            workflow = self._repository.get(name)
        except (OSError, ValueError) as exc:
            return _repository_failure(exc)
        if workflow is None:
            return _failure("no workflow named '%s'." % name, "NOT_FOUND")
        node_id = "workflow:%s:%s" % (
            self._repository.normalize_name(name), uuid4().hex,
        )
        node = self._loop_control.cancellation.create_child(node_id=node_id)

        def durable_cancel_check() -> bool:
            if node.cancelled:
                return True
            if cancel_check is None:
                return False
            try:
                requested = bool(cancel_check())
            except Exception:
                requested = True
            if requested:
                node.cancel(reason="workflow cancellation requested")
            return node.cancelled

        cleanup = getattr(self._runner, "cleanup", None)
        if not callable(cleanup):
            cleanup = lambda _timeout: CleanupResult(
                "workflow-loop", True, True, "loop runner has no external resources"
            )
        self._loop_control.bind(
            node_id,
            "workflow-loop:%s" % node_id,
            cancel=lambda _reason: True,
            cleanup=cleanup,
        )
        options = {
            "max_iterations": max_iterations,
            "stop_on_failure": stop_on_failure,
            "stop_on_success": stop_on_success,
            "delay_seconds": delay_seconds,
        }
        try:
            parameters = inspect.signature(self._runner.run).parameters
            accepts_cancel_check = (
                "cancel_check" in parameters
                or any(
                    parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in parameters.values()
                )
            )
        except (TypeError, ValueError):
            accepts_cancel_check = True
        if accepts_cancel_check:
            options["cancel_check"] = durable_cancel_check
        try:
            result = self._runner.run(
                workflow["actions"],
                dispatch,
                **options,
            )
        except Exception as exc:
            return _failure(
                "workflow runner failed: %s: %s"
                % (exc.__class__.__name__, exc),
                "WORKFLOW_ERROR",
            )
        if not isinstance(result, dict):
            return _failure(
                "workflow runner returned an invalid result",
                "WORKFLOW_ERROR",
            )
        cancellation_evidence = {}
        cancellation_error = ""
        if result.get("cancelled") is True:
            if not node.cancelled:
                node.cancel(reason="workflow loop reported cancellation")
            cleanup_error = ""
            try:
                reports = self._loop_control.cancel_and_cleanup(
                    node_id, reason="workflow cancellation cleanup", timeout=0
                )
                cleanup_complete = bool(reports) and all(
                    report.conforms for report in reports
                )
            except Exception as exc:
                reports = ()
                cleanup_complete = False
                cleanup_error = "%s: %s" % (exc.__class__.__name__, exc)
            cancellation_evidence = {
                "cancelled": True,
                "cleanup_complete": cleanup_complete,
                **({"cleanup_error": cleanup_error} if cleanup_error else {}),
                "reports": [
                    {
                        "target_id": report.target_id,
                        "cancelled": report.cancelled,
                        "quiescent": report.quiescent,
                        "resources_released": report.resources_released,
                        "detail": report.detail,
                    }
                    for report in reports
                ],
            }
            if not cleanup_complete:
                cancellation_error = "CLEANUP_INCOMPLETE"
        header = "workflow: %s\n%s\n" % (
            self._repository.normalize_name(name),
            workflow.get("description") or "(no description)",
        )
        ok = result.get("ok") is True
        return ToolResult(
            ok=ok,
            output=header + self._runner.format(result),
            error_code="" if ok else (cancellation_error or (
                "CANCELLED" if result.get("cancelled") else "WORKFLOW_FAILED"
            )),
            # Legacy MCP workflow_run returned the formatted loop report even
            # when an action failed; keep that wire text while exposing typed
            # failure to application callers.
            evidence={"legacy_raw_output": True, **(
                {"workflow_cancellation": cancellation_evidence}
                if cancellation_evidence else {}
            )},
        )

    def delete(self, name: str) -> ToolResult:
        try:
            existed, path = self._repository.delete(name)
            normalized = self._repository.normalize_name(name)
        except (OSError, ValueError) as exc:
            return _repository_failure(exc)
        if not existed:
            return ToolResult(
                ok=True,
                output=(
                    "No workflow named '%s' existed. File unchanged except "
                    "normalization: %s" % (name, path)
                ),
            )
        return ToolResult(
            ok=True,
            output="Deleted workflow '%s' from %s." % (normalized, path),
        )
