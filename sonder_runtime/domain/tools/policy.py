"""Tool authorization policy (SPEC-5 §15–17).

Pure domain logic — no I/O. Decides whether a tool call is authorized
given the current capabilities and tool effects.
"""
from __future__ import annotations

from typing import Protocol

from .descriptors import ToolCall, ToolDescriptor, ToolEffect, ExecutionClass
from ..common.errors import Forbidden


class ToolPolicy(Protocol):
    def authorize(self, descriptor: ToolDescriptor, call: ToolCall) -> None:
        """Raise Forbidden if the call is not authorized."""
        ...

    def select_execution_class(self, descriptor: ToolDescriptor) -> ExecutionClass:
        """Resolve the execution class for a tool."""
        ...


class GuardedToolPolicy:
    """Default guarded policy: container execution, project scope enforced."""

    def __init__(self, allowed_effects: frozenset[ToolEffect] | None = None):
        self._allowed = allowed_effects or frozenset({
            ToolEffect.READ_FILES,
            ToolEffect.EXECUTE,
        })

    def authorize(self, descriptor: ToolDescriptor, call: ToolCall) -> None:
        denied = descriptor.effects - self._allowed
        if denied:
            names = sorted(e.name for e in denied)
            raise Forbidden(
                f"tool {descriptor.name!r} requires effects {names} "
                f"not allowed by current policy"
            )

    def select_execution_class(self, descriptor: ToolDescriptor) -> ExecutionClass:
        if descriptor.execution_class == ExecutionClass.HOST:
            return ExecutionClass.CONTAINER
        return descriptor.execution_class


class UnrestrictedToolPolicy:
    """--unrestricted-tools: all effects allowed, host execution."""

    def authorize(self, descriptor: ToolDescriptor, call: ToolCall) -> None:
        pass

    def select_execution_class(self, descriptor: ToolDescriptor) -> ExecutionClass:
        if descriptor.execution_class in (ExecutionClass.HOST, ExecutionClass.CONTAINER):
            return ExecutionClass.HOST
        return descriptor.execution_class
