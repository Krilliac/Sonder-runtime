"""Thin MCP tool handlers (SPEC-5 §28).

Each handler: parse tool input → create OperationContext → call service → map errors → result dict.
No business logic, no direct adapter access.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from ...application.context import OperationContext, local_owner_context
from ...application.errors import InvalidInput, SonderError

logger = logging.getLogger(__name__)


def context_for_mcp_call(
    *,
    timeout_seconds: float = 60.0,
) -> OperationContext:
    return local_owner_context(
        correlation_id=uuid.uuid4().hex,
        source="mcp",
        timeout_seconds=timeout_seconds,
    )


def error_result(err: SonderError) -> dict[str, Any]:
    return {"isError": True, "error": err.code, "message": str(err), "retryable": err.retryable}


class McpRecallHandler:
    """recall tool — delegates to RecallService."""

    def __init__(self, recall_service):
        self._recall = recall_service

    def handle(self, arguments: dict[str, Any]) -> dict[str, Any]:
        ctx = context_for_mcp_call()
        task = arguments.get("task", "")
        k = arguments.get("k", 2)
        project = arguments.get("project")
        logger.debug(f"McpRecallHandler.handle k={k} project={project!r}")

        try:
            results = self._recall.recall(task, k=k, project=project)
        except SonderError as e:
            logger.error(f"recall request failed, code={e.code!r}", exc_info=True)
            logger.debug(f"McpRecallHandler recall error code={e.code!r}")
            if e.retryable:
                logger.warning(
                    f"recall request failed with retryable error, code={e.code!r} "
                    f"— upstream dependency may be degraded"
                )
            return error_result(e)

        logger.debug(f"McpRecallHandler recall returned {len(results)} results")
        return {"results": results}


class McpOutcomeHandler:
    """record_outcome tool — delegates to OutcomeService."""

    def __init__(self, outcome_service):
        self._outcome = outcome_service

    def handle(self, arguments: dict[str, Any]) -> dict[str, Any]:
        interaction_id = arguments.get("interaction_id", "")
        signal = arguments.get("signal", "")
        logger.debug(f"McpOutcomeHandler.handle interaction_id={interaction_id!r} signal={signal!r}")

        if not interaction_id or not signal:
            return error_result(InvalidInput("interaction_id and signal required"))

        try:
            score = self._outcome.record(interaction_id, signal)
        except SonderError as e:
            logger.error(
                f"outcome recording failed, interaction_id={interaction_id!r} code={e.code!r}",
                exc_info=True,
            )
            logger.debug(f"McpOutcomeHandler record error code={e.code!r}")
            return error_result(e)

        logger.debug(f"McpOutcomeHandler record score={score}")
        return {"score": score}


class McpToolHandler:
    """Generic tool execution — delegates to ToolService."""

    def __init__(self, tool_service):
        self._tools = tool_service

    def handle(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        from ...application.execution import ToolCall
        ctx = context_for_mcp_call()
        logger.debug(f"McpToolHandler.handle tool_name={tool_name!r}")

        call = ToolCall(
            tool_name=tool_name,
            arguments=arguments,
            call_id=uuid.uuid4().hex,
        )

        try:
            result = self._tools.execute(call, ctx)
        except SonderError as e:
            logger.error(
                f"tool execution failed, tool={tool_name!r} code={e.code!r}",
                exc_info=True,
            )
            logger.debug(f"McpToolHandler execute error tool={tool_name!r} code={e.code!r}")
            if e.retryable:
                logger.warning(
                    f"tool execution failed with retryable error, tool={tool_name!r} "
                    f"code={e.code!r} — transient failure, caller may retry"
                )
            return error_result(e)

        logger.debug(f"McpToolHandler execute completed tool={tool_name!r} is_error={result.is_error}")
        if result.is_error:
            logger.warning(
                f"tool execution returned error result, tool={tool_name!r} "
                f"— tool reported a non-exception failure"
            )
        return {"output": result.output, "isError": result.is_error}
