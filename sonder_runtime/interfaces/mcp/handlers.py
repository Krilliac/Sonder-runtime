"""Thin MCP tool handlers (SPEC-5 §28).

Each handler: parse tool input → create OperationContext → call service → map errors → result dict.
No business logic, no direct adapter access.
"""
from __future__ import annotations

import logging
import math
import uuid
from typing import Any, Mapping

from ...application.context import (
    OperationContext,
    bind_operation_context,
    local_owner_context,
)
from ...application.errors import InternalFailure, InvalidInput, SonderError

logger = logging.getLogger(__name__)


def context_for_mcp_call(
    *,
    timeout_seconds: float = 60.0,
) -> OperationContext:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("MCP timeout_seconds must be a positive finite number")
    return local_owner_context(
        correlation_id=uuid.uuid4().hex,
        source="mcp",
        timeout_seconds=float(timeout_seconds),
    )


def error_result(err: SonderError) -> dict[str, Any]:
    return {"isError": True, "error": err.code, "message": str(err), "retryable": err.retryable}


def _result_is_error(result: Any) -> bool:
    if hasattr(result, "is_error"):
        return bool(result.is_error)
    if hasattr(result, "success"):
        return not bool(result.success)
    if hasattr(result, "ok"):
        return not bool(result.ok)
    return False


class McpRecallHandler:
    """recall tool — delegates to RecallService."""

    def __init__(self, recall_service):
        self._recall = recall_service

    def handle(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(arguments, Mapping):
            return error_result(InvalidInput("arguments must be an object"))
        task = arguments.get("task", "")
        k = arguments.get("k", 2)
        project = arguments.get("project")
        if not isinstance(task, str):
            return error_result(InvalidInput("task must be a string"))
        if isinstance(k, bool) or not isinstance(k, int) or k <= 0:
            return error_result(InvalidInput("k must be a positive integer"))
        if project is not None and not isinstance(project, str):
            return error_result(InvalidInput("project must be a string or null"))

        ctx = context_for_mcp_call()
        logger.debug(f"McpRecallHandler.handle k={k} project={project!r}")

        try:
            with bind_operation_context(ctx):
                results = self._recall.recall(task, k=k, project=project)
        except SonderError as e:
            logger.error(f"recall request failed, code={e.code!r}", exc_info=True)
            if e.retryable:
                logger.warning(
                    f"recall request failed with retryable error, code={e.code!r} "
                    f"— upstream dependency may be degraded"
                )
            return error_result(e)
        except Exception as e:
            logger.error(
                f"unexpected recall error type={type(e).__name__}",
                exc_info=True,
            )
            return error_result(InternalFailure("internal recall failure"))

        logger.debug(f"McpRecallHandler recall returned {len(results)} results")
        return {"results": results}


class McpOutcomeHandler:
    """record_outcome tool — delegates to OutcomeService."""

    def __init__(self, outcome_service):
        self._outcome = outcome_service

    def handle(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(arguments, Mapping):
            return error_result(InvalidInput("arguments must be an object"))
        interaction_id = arguments.get("interaction_id", "")
        signal = arguments.get("signal", "")
        if (
            not isinstance(interaction_id, str)
            or not interaction_id.strip()
            or not isinstance(signal, str)
            or not signal.strip()
        ):
            return error_result(InvalidInput("interaction_id and signal required"))

        ctx = context_for_mcp_call()
        logger.debug(f"McpOutcomeHandler.handle interaction_id={interaction_id!r} signal={signal!r}")

        try:
            with bind_operation_context(ctx):
                score = self._outcome.record(interaction_id, signal)
        except SonderError as e:
            logger.error(
                f"outcome recording failed, interaction_id={interaction_id!r} code={e.code!r}",
                exc_info=True,
            )
            return error_result(e)
        except Exception as e:
            logger.error(
                f"unexpected outcome recording error type={type(e).__name__}",
                exc_info=True,
            )
            return error_result(InternalFailure("internal outcome failure"))

        logger.debug(f"McpOutcomeHandler record score={score}")
        return {"score": score}


class McpToolHandler:
    """Generic tool execution — delegates to ToolService."""

    def __init__(self, tool_service):
        self._tools = tool_service

    def handle(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        from ...application.execution import ToolCall
        if not isinstance(tool_name, str) or not tool_name.strip():
            return error_result(InvalidInput("tool_name must be a non-empty string"))
        if not isinstance(arguments, Mapping):
            return error_result(InvalidInput("arguments must be an object"))
        ctx = context_for_mcp_call()
        logger.debug(f"McpToolHandler.handle tool_name={tool_name!r}")

        call = ToolCall(
            tool_name=tool_name,
            arguments=dict(arguments),
            call_id=uuid.uuid4().hex,
        )

        try:
            with bind_operation_context(ctx):
                result = self._tools.execute(call, ctx)
        except SonderError as e:
            logger.error(
                f"tool execution failed, tool={tool_name!r} code={e.code!r}",
                exc_info=True,
            )
            if e.retryable:
                logger.warning(
                    f"tool execution failed with retryable error, tool={tool_name!r} "
                    f"code={e.code!r} — transient failure, caller may retry"
                )
            return error_result(e)
        except Exception as e:
            logger.error(
                f"unexpected tool execution error tool={tool_name!r} type={type(e).__name__}",
                exc_info=True,
            )
            return error_result(InternalFailure("internal tool execution failure"))

        is_error = _result_is_error(result)
        logger.debug(f"McpToolHandler execute completed tool={tool_name!r} is_error={is_error}")
        if is_error:
            logger.warning(
                f"tool execution returned error result, tool={tool_name!r} "
                f"— tool reported a non-exception failure"
            )
        payload: dict[str, Any] = {"output": getattr(result, "output", ""), "isError": is_error}
        if is_error:
            err_text = getattr(result, "error", "") or getattr(result, "error_code", "")
            if isinstance(err_text, str) and err_text:
                payload["error"] = err_text
        return payload
