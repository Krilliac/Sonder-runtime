"""Application port for the model-visible tool registry.

This module contains the contract only.  It does not discover tools, perform
I/O, authorize a call, or execute a tool.  Adapters may register descriptors;
application services validate calls before they cross the policy boundary.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol

from ...domain.common.errors import Conflict, Forbidden, InvalidInput, NotFound
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


@dataclass(frozen=True)
class ToolSchemaSelection:
    """Immutable per-turn set of tool schemas made visible to a caller.

    The executable inventory remains owned by the registry.  A selection only
    controls which registered descriptors may be admitted for this turn; it
    is deliberately a value object so callers can carry it across bounded
    context resets without copying mutable registry state.
    """

    visible_names: frozenset[str] = frozenset()
    selection_id: str = ""
    summary_first: bool = True
    on_demand: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "visible_names", frozenset(self.visible_names))
        if any(not isinstance(name, str) or not name.strip() for name in self.visible_names):
            raise InvalidInput("visible tool names must be non-empty strings")
        if not isinstance(self.selection_id, str):
            raise InvalidInput("tool schema selection_id must be text")

    def allows(self, name: str) -> bool:
        return name in self.visible_names

    def marker(self) -> dict[str, Any]:
        """Return the explicit, model-safe marker for catalog consumers."""
        return {
            "schema": "sonder-tool-schema-selection-v1",
            "selection_id": self.selection_id,
            "summary_first": self.summary_first,
            "on_demand": self.on_demand,
            "visible_names": tuple(sorted(self.visible_names)),
        }


@dataclass(frozen=True)
class ExecutableToolInventory:
    """Stable descriptor snapshot, independent of later registrations."""

    descriptors: tuple[ToolDescriptor, ...]

    def __post_init__(self) -> None:
        # ToolDescriptor is frozen, but its JSON schema is a dict.  Copy it at
        # snapshot creation so later registry or caller mutations cannot alter
        # this inventory's captured descriptor metadata.
        object.__setattr__(
            self,
            "descriptors",
            tuple(
                ToolDescriptor(
                    name=item.name,
                    description=item.description,
                    input_schema=deepcopy(item.input_schema),
                    effects=frozenset(item.effects),
                    execution_class=item.execution_class,
                )
                for item in self.descriptors
            ),
        )

    def get(self, name: str) -> ToolDescriptor | None:
        return next((item for item in self.descriptors if item.name == name), None)

    def list_all(self) -> tuple[ToolDescriptor, ...]:
        return self.descriptors

    def admit(self, name: str, selection: ToolSchemaSelection | None = None) -> ToolDescriptor:
        descriptor = self.get(name)
        if descriptor is None:
            raise NotFound(f"unknown tool {name!r}")
        if selection is not None and not selection.allows(name):
            raise Forbidden(f"tool {name!r} is not visible in the active schema selection")
        return descriptor


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
    def admit(self, name: str, selection: ToolSchemaSelection | None = None) -> ToolDescriptor: ...


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

    def executable_inventory(self) -> ExecutableToolInventory:
        """Return a stable executable snapshot, independent of visibility."""
        return ExecutableToolInventory(tuple(self._tools.values()))

    def admit(self, name: str, selection: ToolSchemaSelection | None = None) -> ToolDescriptor:
        return self.executable_inventory().admit(name, selection)

    def require(self, name: str) -> ToolDescriptor:
        descriptor = self.get(name)
        if descriptor is None:
            raise NotFound(f"unknown tool {name!r}")
        return descriptor

    def list_all(self) -> tuple[ToolDescriptor, ...]:
        return tuple(self._tools.values())


__all__ = [
    "ExecutableToolInventory", "InMemoryToolRegistry", "ToolCall", "ToolDescriptor",
    "ToolRegistry", "ToolSchemaSelection", "validate_tool_call",
]
