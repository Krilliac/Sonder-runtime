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
    return result.output if result.ok else "ERROR: %s" % result.output


class WorkflowService:
    def __init__(self, repository: WorkflowRepository, runner: LoopRunner) -> None:
        self._repository = repository
        self._runner = runner

    def list(self) -> ToolResult:
        workflows, path = self._repository.ensure()
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
        except json.JSONDecodeError as exc:
            return _failure("actions_json is not valid JSON: %s" % exc)
        actions = parsed.get("actions") if isinstance(parsed, dict) else parsed
        try:
            workflow, path = self._repository.save(name, actions, description)
            normalized = self._repository.normalize_name(name)
        except ValueError as exc:
            return _failure(exc)
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
    ) -> ToolResult:
        try:
            workflow = self._repository.get(name)
        except ValueError as exc:
            return _failure(exc)
        if workflow is None:
            return _failure("no workflow named '%s'." % name, "NOT_FOUND")
        result = self._runner.run(
            workflow["actions"],
            dispatch,
            max_iterations=max_iterations,
            stop_on_failure=stop_on_failure,
            stop_on_success=stop_on_success,
            delay_seconds=delay_seconds,
        )
        header = "workflow: %s\n%s\n" % (
            self._repository.normalize_name(name),
            workflow.get("description") or "(no description)",
        )
        return ToolResult(ok=True, output=header + self._runner.format(result))

    def delete(self, name: str) -> ToolResult:
        try:
            existed, path = self._repository.delete(name)
            normalized = self._repository.normalize_name(name)
        except ValueError as exc:
            return _failure(exc)
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
