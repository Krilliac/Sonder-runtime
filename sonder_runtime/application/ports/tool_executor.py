"""Guarded tool execution port (SPEC-3 section 4)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..context import OperationContext


@dataclass(frozen=True)
class ToolCall:
    tool: str
    arguments: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    output: str = ""
    error_code: str = ""
    evidence: dict = field(default_factory=dict)


class ToolExecutor(Protocol):
    """Every mutating tool path passes through here. Workspace containment,
    executable/argument/timeout/output rules, and cancellation are enforced
    behind this port — application and domain code never spawn processes or
    mutate files directly."""

    def execute(
        self, call: ToolCall, context: OperationContext
    ) -> ToolResult: ...
