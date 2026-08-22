"""Application port for the model-visible tool registry.

This module contains the contract only.  It does not discover tools, perform
I/O, authorize a call, or execute a tool.  Adapters may register descriptors;
application services validate calls before they cross the policy boundary.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol

from ...domain.common.errors import Conflict, InvalidInput, NotFound
from ...domain.tools.descriptors import ExecutionClass, ToolEffect


@dataclass(frozen=True)
class ToolDescriptor:
    """Stable metadata exposed to model/tool callers.

    ``input_schema`` is a JSON-Schema-shaped object.  Validation is performed
    by :func:`validate_tool_call`; executors must not infer missing policy from
    this metadata.
    """

    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    effects: frozenset[ToolEffect] = field(default_factory=frozenset)
    execution_class: ExecutionClass = ExecutionClass.PURE

    def __post_init__(self) -> None:
        if not self.name or self.name.strip() != self.name:
            raise InvalidInput("tool name must be a non-empty trimmed string")
        if not isinstance(self.input_schema, dict):
            raise InvalidInput("tool input_schema must be an object")
        if self.input_schema and self.input_schema.get("type", "object") != "object":
            raise InvalidInput("tool input_schema must describe an object")


@dataclass(frozen=True)
class ToolCall:
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    call_id: str = ""


def _validate(value: Any, schema: dict[str, Any], path: str) -> None:
    expected = schema.get("type")
    checks = {
        "object": lambda v: isinstance(v, dict),
        "array": lambda v: isinstance(v, list),
        "string": lambda v: isinstance(v, str),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
        "boolean": lambda v: isinstance(v, bool),
        "null": lambda v: v is None,
    }
    if expected in checks and not checks[expected](value):
        raise InvalidInput(f"{path} must be {expected}")
    if "enum" in schema and value not in schema["enum"]:
        raise InvalidInput(f"{path} is not an allowed value")
    if "const" in schema and value != schema["const"]:
        raise InvalidInput(f"{path} must equal the declared constant")
    if isinstance(value, dict):
        required = schema.get("required", ())
        missing = [key for key in required if key not in value]
        if missing:
            raise InvalidInput(f"{path} is missing required field(s): {', '.join(missing)}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties", True) is False:
            unexpected = sorted(set(value) - set(properties))
            if unexpected:
                raise InvalidInput(f"{path} contains unknown field(s): {', '.join(unexpected)}")
        for key, child_schema in properties.items():
            if key in value:
                _validate(value[key], child_schema, f"{path}.{key}")
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            _validate(item, schema["items"], f"{path}[{index}]")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            raise InvalidInput(f"{path} is shorter than minLength")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise InvalidInput(f"{path} is longer than maxLength")


def validate_tool_call(descriptor: ToolDescriptor, call: ToolCall) -> None:
    """Validate a call against its descriptor, raising ``InvalidInput``."""
    if call.tool_name != descriptor.name:
        raise InvalidInput("tool call name does not match its descriptor")
    if not isinstance(call.arguments, dict):
        raise InvalidInput("tool arguments must be an object")
    _validate(call.arguments, descriptor.input_schema, "arguments")


class ToolRegistry(Protocol):
    """Read-only lookup boundary plus lifecycle registration."""

    def get(self, name: str) -> ToolDescriptor | None: ...
    def list_all(self) -> tuple[ToolDescriptor, ...]: ...


class InMemoryToolRegistry:
    """Small deterministic registry useful for composition roots and tests."""

    def __init__(self, descriptors: Iterable[ToolDescriptor] = ()) -> None:
        self._tools: dict[str, ToolDescriptor] = {}
        for descriptor in descriptors:
            self.register(descriptor)

    def register(self, descriptor: ToolDescriptor) -> None:
        if descriptor.name in self._tools:
            raise Conflict(f"tool {descriptor.name!r} is already registered")
        self._tools[descriptor.name] = descriptor

    def get(self, name: str) -> ToolDescriptor | None:
        return self._tools.get(name)

    def require(self, name: str) -> ToolDescriptor:
        descriptor = self.get(name)
        if descriptor is None:
            raise NotFound(f"unknown tool {name!r}")
        return descriptor

    def list_all(self) -> tuple[ToolDescriptor, ...]:
        return tuple(self._tools.values())


__all__ = ["InMemoryToolRegistry", "ToolCall", "ToolDescriptor", "ToolRegistry", "validate_tool_call"]
