"""Saved-workflow use cases independent of MCP and legacy storage modules."""
from __future__ import annotations

from collections.abc import Callable
import json

from ..ports.tool_executor import ToolResult
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
    def __init__(self, repository: WorkflowRepository, runner: LoopRunner) -> None:
        self._repository = repository
        self._runner = runner

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
        try:
            options = {
                "max_iterations": max_iterations,
                "stop_on_failure": stop_on_failure,
                "stop_on_success": stop_on_success,
                "delay_seconds": delay_seconds,
            }
            if cancel_check is not None:
                options["cancel_check"] = cancel_check
            result = self._runner.run(
                workflow["actions"],
                dispatch,
                **options,
            )
        except (OSError, ValueError) as exc:
            return _failure(exc, "WORKFLOW_ERROR")
        header = "workflow: %s\n%s\n" % (
            self._repository.normalize_name(name),
            workflow.get("description") or "(no description)",
        )
        ok = result.get("ok") is True
        return ToolResult(
            ok=ok,
            output=header + self._runner.format(result),
            error_code="" if ok else (
                "CANCELLED" if result.get("cancelled") else "WORKFLOW_FAILED"
            ),
            # Legacy MCP workflow_run returned the formatted loop report even
            # when an action failed; keep that wire text while exposing typed
            # failure to application callers.
            evidence={"legacy_raw_output": True},
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
