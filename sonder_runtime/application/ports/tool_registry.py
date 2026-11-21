"""Application port for the model-visible tool registry.

This module contains the contract only.  It does not discover tools, perform
I/O, authorize a call, or execute a tool.  Adapters may register descriptors;
application services validate calls before they cross the policy boundary.
"""
from __future__ import annotations

import json
import math
import re
from copy import deepcopy
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Callable, Iterable, Protocol

from ...domain.common.errors import Conflict, Forbidden, InvalidInput, NotFound
from ...domain.tools.descriptors import ExecutionClass, ToolEffect
from ...domain.tools.traits import ToolTraits, traits_from_effects

# The schema dialect this runtime enforces.  It is a deliberately bounded
# subset of JSON Schema, not a full implementation: every assertion keyword
# listed here is checked by ``validate_tool_call``, and a descriptor that uses
# any other keyword is refused at construction, so a tool can never advertise
# a constraint the gate does not enforce.
#
# Where the subset is narrower than JSON Schema:
# - ``pattern`` must be written anchored (``^...$``) and is matched against the
#   whole string with Python's ``re`` in ASCII mode.  A top-level alternation
#   is therefore matched as a whole-string alternation.
# - ``integer`` accepts Python ``int`` only (``1.0`` is not an integer), and
#   ``exclusiveMinimum``/``exclusiveMaximum`` are numbers, never booleans.
# - ``items`` is one schema for every element; ``additionalProperties`` is a
#   boolean or one schema for every undeclared key.
# - NaN and infinities are refused everywhere in the arguments.
TOOL_SCHEMA_DIALECT = "sonder-tool-schema-subset-v1"
SONDER_TOOL_SCHEMA_SUBSET_V1 = frozenset({
    "additionalProperties", "allOf", "anyOf", "const", "enum",
    "exclusiveMaximum", "exclusiveMinimum", "items", "maxItems", "maxLength",
    "maxProperties", "maximum", "minItems", "minLength", "minProperties",
    "minimum", "oneOf", "pattern", "properties", "required", "type",
})
# Keywords that describe a value without constraining it.  They are carried
# through to callers and never evaluated.
TOOL_SCHEMA_ANNOTATIONS = frozenset({
    "$comment", "$schema", "default", "deprecated", "description", "examples",
    "readOnly", "title", "writeOnly",
})
# Bounds applied before any recursive work.  The schema bound keeps a
# descriptor (and so validation recursion) shallow; the argument bounds are
# checked with an explicit stack, so hostile nesting cannot exhaust the
# interpreter stack.
MAX_TOOL_SCHEMA_DEPTH = 32
MAX_TOOL_ARGUMENT_DEPTH = 64
MAX_TOOL_ARGUMENT_NODES = 200_000
MAX_TOOL_ARGUMENT_BYTES = 16 * 1024 * 1024

_TYPE_CHECKS: dict[str, Callable[[Any], bool]] = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}
_COUNT_KEYWORDS = frozenset({
    "maxItems", "maxLength", "maxProperties", "minItems", "minLength", "minProperties",
})
_BOUND_KEYWORDS = frozenset({
    "exclusiveMaximum", "exclusiveMinimum", "maximum", "minimum",
})
_COMBINATORS = frozenset({"allOf", "anyOf", "oneOf"})


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


@lru_cache(maxsize=512)
def _compiled_pattern(pattern: str) -> re.Pattern[str]:
    """Compile one schema pattern; shared by registration and validation."""
    body = pattern[:-1]
    escaped_dollar = (len(body) - len(body.rstrip("\\"))) % 2 == 1
    if not pattern.startswith("^") or not pattern.endswith("$") or escaped_dollar:
        raise InvalidInput("tool schema pattern must be anchored with ^ and $")
    try:
        return re.compile(pattern, re.ASCII)
    except (re.error, RecursionError, OverflowError) as exc:
        raise InvalidInput("tool schema pattern is not a valid expression") from exc


def _check_schema(schema: Any, path: str, depth: int) -> None:
    if not isinstance(schema, dict):
        raise InvalidInput(f"{path} must be a schema object")
    if depth > MAX_TOOL_SCHEMA_DEPTH:
        raise InvalidInput(f"tool schema is nested deeper than {MAX_TOOL_SCHEMA_DEPTH} levels")
    for keyword, spec in schema.items():
        if not isinstance(keyword, str):
            raise InvalidInput(f"{path} has a non-text schema keyword")
        if keyword in TOOL_SCHEMA_ANNOTATIONS:
            continue
        if keyword not in SONDER_TOOL_SCHEMA_SUBSET_V1:
            raise InvalidInput(
                f"{path} uses schema keyword {keyword!r}, which {TOOL_SCHEMA_DIALECT} does not enforce"
            )
        here = f"{path}.{keyword}"
        if keyword == "type":
            names = [spec] if isinstance(spec, str) else spec
            if (
                not isinstance(names, list) or not names
                or any(not isinstance(name, str) or name not in _TYPE_CHECKS for name in names)
                or len(set(names)) != len(names)
            ):
                raise InvalidInput(f"{here} must be a known type name or a list of distinct type names")
        elif keyword == "enum":
            if not isinstance(spec, (list, tuple)):
                raise InvalidInput(f"{here} must be an array")
        elif keyword == "required":
            if not isinstance(spec, (list, tuple)) or any(not isinstance(name, str) for name in spec):
                raise InvalidInput(f"{here} must be an array of field names")
        elif keyword == "properties":
            if not isinstance(spec, dict) or any(not isinstance(name, str) for name in spec):
                raise InvalidInput(f"{here} must be an object keyed by field name")
            for name, child in spec.items():
                _check_schema(child, f"{here}.{name}", depth + 1)
        elif keyword == "additionalProperties":
            if not isinstance(spec, bool):
                _check_schema(spec, here, depth + 1)
        elif keyword == "items":
            _check_schema(spec, here, depth + 1)
        elif keyword in _COMBINATORS:
            if not isinstance(spec, (list, tuple)) or not spec:
                raise InvalidInput(f"{here} must be a non-empty array of schemas")
            for index, child in enumerate(spec):
                _check_schema(child, f"{here}[{index}]", depth + 1)
        elif keyword in _COUNT_KEYWORDS:
            if not isinstance(spec, int) or isinstance(spec, bool) or spec < 0:
                raise InvalidInput(f"{here} must be a non-negative integer")
        elif keyword in _BOUND_KEYWORDS:
            if not _is_number(spec) or (isinstance(spec, float) and not math.isfinite(spec)):
                raise InvalidInput(f"{here} must be a finite number")
        elif keyword == "pattern":
            if not isinstance(spec, str):
                raise InvalidInput(f"{here} must be text")
            _compiled_pattern(spec)
        # ``const`` accepts any value and needs no shape check.


def check_schema_supported(schema: dict[str, Any]) -> None:
    """Refuse a schema this runtime cannot fully enforce.

    Raises ``InvalidInput`` for any assertion keyword outside
    ``SONDER_TOOL_SCHEMA_SUBSET_V1``, a malformed keyword value (a bound that
    is not a finite number, a pattern that does not compile or is not
    anchored), or nesting deeper than ``MAX_TOOL_SCHEMA_DEPTH``.  Pure; safe
    to call from any thread.
    """
    _check_schema(schema, "input_schema", 1)


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
    traits: ToolTraits | None = None

    def __post_init__(self) -> None:
        if self.traits is None:
            object.__setattr__(self, "traits", traits_from_effects(self.effects))
        elif not isinstance(self.traits, ToolTraits):
            raise InvalidInput("tool traits must be ToolTraits")
        if not self.name or self.name.strip() != self.name:
            raise InvalidInput("tool name must be a non-empty trimmed string")
        if not isinstance(self.input_schema, dict):
            raise InvalidInput("tool input_schema must be an object")
        if self.input_schema and self.input_schema.get("type", "object") != "object":
            raise InvalidInput("tool input_schema must describe an object")
        check_schema_supported(self.input_schema)


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
                    traits=item.traits,
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


_TEXT_CHUNK = 1 << 16


def _text_bytes(text: str, budget: int) -> int:
    """Exact size of ``text`` as a quoted JSON string with ``ensure_ascii``.

    That is the form ``permission_modes.call_digest`` and the audit encode
    arguments in, and the largest one: a control character, ``DEL``, a lone
    surrogate or any non-ASCII character becomes a 6-byte ``\\uXXXX`` escape,
    and a character above U+FFFF a 12-byte surrogate pair.  Every code point
    costs at least one byte, so an oversized string is refused before it is
    encoded; the rest is encoded in chunks, so at most ``12 * _TEXT_CHUNK``
    bytes are held at once, and encoding stops as soon as the budget is spent.
    """
    if len(text) > budget:
        raise InvalidInput("tool arguments exceed the size limit")
    size = 2
    for start in range(0, len(text), _TEXT_CHUNK):
        size += len(json.dumps(text[start:start + _TEXT_CHUNK], ensure_ascii=True)) - 2
        if size > budget:
            raise InvalidInput("tool arguments exceed the size limit")
    return size


def _key_bytes(key: Any, budget: int) -> int:
    # ``json.dumps`` writes a non-string key as its quoted scalar form.
    if isinstance(key, str):
        return _text_bytes(key, budget)
    if isinstance(key, int) and not isinstance(key, bool):
        return key.bit_length() // 3 + 4
    if isinstance(key, float):
        return 26
    return 8


def _check_argument_limits(arguments: dict[str, Any]) -> None:
    """Bound depth, node count and encoded size; refuse NaN and infinities.

    Iterative on purpose: this runs before the recursive validator, so a
    deeply nested or self-referential argument is refused here instead of
    raising ``RecursionError`` there.  For JSON-native values the size is an
    upper bound on the compact ``ensure_ascii`` JSON encoding: strings are
    charged their exact escaped length, numbers a digit-count ceiling and
    containers their brackets and separators.  It is not an exact byte count,
    and an object outside the JSON data model is charged a flat 8 bytes.
    """
    budget = MAX_TOOL_ARGUMENT_BYTES
    nodes = 1
    stack: list[tuple[Any, int]] = [(arguments, 1)]
    while stack:
        value, depth = stack.pop()
        if isinstance(value, str):
            budget -= _text_bytes(value, budget)
        elif isinstance(value, float):
            if not math.isfinite(value):
                raise InvalidInput("tool arguments contain a non-finite number")
            budget -= 24
        elif isinstance(value, int) and not isinstance(value, bool):
            budget -= value.bit_length() // 3 + 2
        elif isinstance(value, (dict, list, tuple)):
            if depth > MAX_TOOL_ARGUMENT_DEPTH:
                raise InvalidInput(
                    f"tool arguments are nested deeper than {MAX_TOOL_ARGUMENT_DEPTH} levels"
                )
            nodes += len(value)
            if nodes > MAX_TOOL_ARGUMENT_NODES:
                raise InvalidInput("tool arguments contain too many values")
            if isinstance(value, dict):
                # Braces, one colon per key and the commas between members.
                budget -= 2 + 2 * len(value)
                for key, child in value.items():
                    budget -= _key_bytes(key, budget)
                    if budget < 0:
                        raise InvalidInput("tool arguments exceed the size limit")
                    stack.append((child, depth + 1))
            else:
                budget -= 2 + len(value)
                stack.extend((child, depth + 1) for child in value)
        else:
            budget -= 8
        if budget < 0:
            raise InvalidInput("tool arguments exceed the size limit")


def _passes(value: Any, schema: dict[str, Any], path: str) -> bool:
    try:
        _validate(value, schema, path)
    except InvalidInput:
        return False
    return True


def _validate(value: Any, schema: dict[str, Any], path: str) -> None:
    expected = schema.get("type")
    if expected is not None:
        names = (expected,) if isinstance(expected, str) else tuple(expected)
        # An unknown type name matches nothing, so it fails closed.
        if not any(name in _TYPE_CHECKS and _TYPE_CHECKS[name](value) for name in names):
            raise InvalidInput(f"{path} must be {' or '.join(str(name) for name in names)}")
    if "enum" in schema and value not in schema["enum"]:
        raise InvalidInput(f"{path} is not an allowed value")
    if "const" in schema and value != schema["const"]:
        raise InvalidInput(f"{path} must equal the declared constant")
    for index, branch in enumerate(schema.get("allOf", ())):
        _validate(value, branch, f"{path}<allOf {index}>")
    if "anyOf" in schema and not any(_passes(value, branch, path) for branch in schema["anyOf"]):
        raise InvalidInput(f"{path} matches none of the allowed alternatives")
    if "oneOf" in schema:
        matched = sum(1 for branch in schema["oneOf"] if _passes(value, branch, path))
        if matched != 1:
            raise InvalidInput(
                f"{path} must match exactly one allowed alternative, but matches {matched}"
            )
    if isinstance(value, dict):
        required = schema.get("required", ())
        missing = [key for key in required if key not in value]
        if missing:
            raise InvalidInput(f"{path} is missing required field(s): {', '.join(missing)}")
        if len(value) < schema.get("minProperties", 0):
            raise InvalidInput(f"{path} has fewer fields than minProperties")
        if "maxProperties" in schema and len(value) > schema["maxProperties"]:
            raise InvalidInput(f"{path} has more fields than maxProperties")
        properties = schema.get("properties", {})
        additional = schema.get("additionalProperties", True)
        if additional is False:
            unexpected = sorted(str(key) for key in set(value) - set(properties))
            if unexpected:
                raise InvalidInput(f"{path} contains unknown field(s): {', '.join(unexpected)}")
        elif additional is not True:
            for key, item in value.items():
                if key not in properties:
                    _validate(item, additional, f"{path}.<additional field>")
        for key, child_schema in properties.items():
            if key in value:
                _validate(value[key], child_schema, f"{path}.{key}")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            raise InvalidInput(f"{path} has fewer items than minItems")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise InvalidInput(f"{path} has more items than maxItems")
        if "items" in schema:
            for index, item in enumerate(value):
                _validate(item, schema["items"], f"{path}[{index}]")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            raise InvalidInput(f"{path} is shorter than minLength")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise InvalidInput(f"{path} is longer than maxLength")
        if "pattern" in schema and _compiled_pattern(schema["pattern"]).fullmatch(value) is None:
            raise InvalidInput(f"{path} does not match the required pattern")
    if _is_number(value):
        if isinstance(value, float) and not math.isfinite(value):
            raise InvalidInput(f"{path} must be a finite number")
        # Written as negated comparisons so an unordered value fails closed.
        if "minimum" in schema and not value >= schema["minimum"]:
            raise InvalidInput(f"{path} is below minimum")
        if "maximum" in schema and not value <= schema["maximum"]:
            raise InvalidInput(f"{path} is above maximum")
        if "exclusiveMinimum" in schema and not value > schema["exclusiveMinimum"]:
            raise InvalidInput(f"{path} is not above exclusiveMinimum")
        if "exclusiveMaximum" in schema and not value < schema["exclusiveMaximum"]:
            raise InvalidInput(f"{path} is not below exclusiveMaximum")


def validate_tool_arguments(schema: dict[str, Any], arguments: Any) -> None:
    """Validate raw arguments against a raw schema, raising ``InvalidInput``.

    For callers that hold a schema outside a :class:`ToolDescriptor`.  The
    schema is checked against the supported subset on every call, because it
    has not been through descriptor construction.
    """
    if not isinstance(arguments, dict):
        raise InvalidInput("tool arguments must be an object")
    check_schema_supported(schema)
    _check_argument_limits(arguments)
    _validate(arguments, schema, "arguments")


def validate_tool_call(descriptor: ToolDescriptor, call: ToolCall) -> None:
    """Validate a call against its descriptor, raising ``InvalidInput``.

    Size, depth and finite-number limits are applied first; only then is the
    schema evaluated.  No other exception type is raised for bad arguments.
    """
    if call.tool_name != descriptor.name:
        raise InvalidInput("tool call name does not match its descriptor")
    if not isinstance(call.arguments, dict):
        raise InvalidInput("tool arguments must be an object")
    _check_argument_limits(call.arguments)
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
    "ExecutableToolInventory", "InMemoryToolRegistry", "MAX_TOOL_ARGUMENT_BYTES",
    "MAX_TOOL_ARGUMENT_DEPTH", "MAX_TOOL_ARGUMENT_NODES", "MAX_TOOL_SCHEMA_DEPTH",
    "SONDER_TOOL_SCHEMA_SUBSET_V1", "TOOL_SCHEMA_ANNOTATIONS", "TOOL_SCHEMA_DIALECT",
    "ToolCall", "ToolDescriptor", "ToolRegistry", "ToolSchemaSelection",
    "check_schema_supported", "validate_tool_arguments", "validate_tool_call",
]
