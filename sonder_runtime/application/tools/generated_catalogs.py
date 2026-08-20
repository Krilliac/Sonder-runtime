"""Deterministic generated catalogs for MCP, OpenAI, CLI, and clients.

The generator is deliberately an application-side projection.  Tool metadata
comes from the typed tool registry and event metadata comes from the typed
durable-event vocabulary; no legacy module, transport, or provider is
imported.  Every projection is derived from one canonical intermediate form,
so a digest can be used by clients to detect stale schemas.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping

from ...domain.common.events import EventKind, payload_schema
from ...domain.tools.descriptors import ExecutionClass, ToolEffect
from ..ports.tool_registry import ToolDescriptor


class CatalogLimitError(ValueError):
    """Raised when a catalog would exceed its explicit transport budget."""


@dataclass(frozen=True)
class CatalogLimits:
    max_tools: int = 256
    max_events: int = 128
    max_commands: int = 256
    max_bytes: int = 256_000

    def __post_init__(self) -> None:
        if any(isinstance(value, bool) or value <= 0 for value in (
            self.max_tools, self.max_events, self.max_commands, self.max_bytes
        )):
            raise ValueError("catalog limits must be positive integers")


@dataclass(frozen=True)
class CatalogBundle:
    """The four bounded projections plus their freshness digest."""

    mcp: Mapping[str, Any]
    openai: Mapping[str, Any]
    cli: Mapping[str, Any]
    client: Mapping[str, Any]
    digest: str
    permissions: Mapping[str, Any] = field(default_factory=dict)
    conformance: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "client": dict(self.client),
            "cli": dict(self.cli),
            "digest": self.digest,
            "mcp": dict(self.mcp),
            "openai": dict(self.openai),
            "permissions": dict(self.permissions),
            "conformance": dict(self.conformance),
        }


def _json_type(expected: Any) -> str:
    if expected is str:
        return "string"
    if expected is bool:
        return "boolean"
    if expected is int:
        return "integer"
    if expected is float:
        return "number"
    if expected in (list, tuple):
        return "array"
    if expected in (dict, Mapping):
        return "object"
    if isinstance(expected, tuple):
        types = [_json_type(item) for item in expected if item is not type(None)]
        return types[0] if len(set(types)) == 1 else "string"
    return "object"


def _event_schema(kind: EventKind) -> dict[str, Any]:
    contract = payload_schema(kind)
    properties: dict[str, Any] = {}
    for name, expected in {**contract.required, **contract.optional}.items():
        properties[name] = {"type": _json_type(expected)}
    return {
        "name": kind.value,
        "required": sorted(contract.required),
        "schema": {
            "type": "object",
            "properties": properties,
            "required": sorted(contract.required),
            "additionalProperties": False,
        },
    }


def _effect_name(effect: Any) -> str:
    return effect.name.lower() if isinstance(effect, Enum) else str(effect)


def _descriptor(tool: Any) -> ToolDescriptor:
    if not isinstance(tool, ToolDescriptor):
        # Accept structurally compatible registry implementations while still
        # validating through the application contract's constructor.
        tool = ToolDescriptor(
            name=str(tool.name),
            description=str(getattr(tool, "description", "")),
            input_schema=dict(getattr(tool, "input_schema", {}) or {}),
            effects=frozenset(getattr(tool, "effects", frozenset())),
            execution_class=getattr(tool, "execution_class", ExecutionClass.PURE),
        )
    return tool


def _commands(commands: Iterable[Any], limit: int) -> tuple[dict[str, Any], ...]:
    result = []
    for command in commands:
        if isinstance(command, str):
            name, summary, category, risk = command, "", "", ""
        elif isinstance(command, Mapping):
            name = str(command.get("name", ""))
            summary = str(command.get("summary", command.get("description", "")))
            category = str(command.get("category", ""))
            risk = str(command.get("risk", ""))
        else:
            name = str(getattr(command, "name", ""))
            summary = str(getattr(command, "summary", getattr(command, "description", "")))
            category = str(getattr(command, "category", ""))
            risk = str(getattr(command, "risk", ""))
        if not name.strip():
            raise ValueError("catalog command names must be non-empty")
        result.append({"category": category, "name": name, "risk": risk, "summary": summary})
        if len(result) > limit:
            raise CatalogLimitError("command catalog exceeds max_commands")
    return tuple(sorted(result, key=lambda item: item["name"]))


class GeneratedCatalogs:
    """Build one canonical, deterministic catalog bundle from typed sources."""

    SCHEMA_VERSION = "1"

    @classmethod
    def generate(
        cls,
        registry: Any,
        *,
        commands: Iterable[Any] = (),
        event_kinds: Iterable[EventKind | str] = EventKind,
        limits: CatalogLimits | None = None,
    ) -> CatalogBundle:
        limits = limits or CatalogLimits()
        source = registry.list_all() if hasattr(registry, "list_all") else tuple(registry)
        tools = tuple(sorted((_descriptor(item) for item in source), key=lambda item: item.name))
        if len(tools) > limits.max_tools:
            raise CatalogLimitError("tool catalog exceeds max_tools")
        if len({tool.name for tool in tools}) != len(tools):
            raise ValueError("tool names must be unique")
        normalized_events = tuple(sorted(
            (EventKind(item) if not isinstance(item, EventKind) else item for item in event_kinds),
            key=lambda item: item.value,
        ))
        if len(normalized_events) > limits.max_events:
            raise CatalogLimitError("event catalog exceeds max_events")
        event_contracts = tuple(_event_schema(item) for item in normalized_events)
        tool_contracts = tuple({
            "description": tool.description,
            "effects": sorted(_effect_name(effect) for effect in tool.effects),
            "execution_class": tool.execution_class.name.lower(),
            "input_schema": tool.input_schema,
            "name": tool.name,
        } for tool in tools)
        command_contracts = _commands(commands, limits.max_commands)
        canonical = {
            "commands": command_contracts,
            "events": event_contracts,
            "schema_version": cls.SCHEMA_VERSION,
            "tools": tool_contracts,
        }
        digest = hashlib.sha256(cls._json(canonical).encode("utf-8")).hexdigest()
        mcp = {"tools": tuple({"description": t["description"], "inputSchema": t["input_schema"], "name": t["name"]} for t in tool_contracts)}
        openai = {"tools": tuple({"function": {"description": t["description"], "name": t["name"], "parameters": t["input_schema"]}, "type": "function"} for t in tool_contracts)}
        cli = {"commands": command_contracts, "tools": tuple({"name": t["name"], "summary": t["description"]} for t in tool_contracts)}
        client = {"digest": digest, "events": event_contracts, "schema_version": cls.SCHEMA_VERSION, "tools": tool_contracts}
        permissions = {
            "schema": "sonder-tool-permissions-v1",
            "tools": tuple({
                "execution_class": t["execution_class"],
                "effects": t["effects"],
                "name": t["name"],
            } for t in tool_contracts),
        }
        conformance = {
            "schema": "sonder-catalog-conformance-v1",
            "tools": tuple({
                "name": t["name"],
                "surfaces": {"mcp": t["name"], "openai": t["name"], "cli": t["name"], "client": t["name"]},
                "input_schema": t["input_schema"],
            } for t in tool_contracts),
            "events": tuple(event_contracts),
        }
        bundle = CatalogBundle(
            mcp=mcp, openai=openai, cli=cli, client=client, digest=digest,
            permissions=permissions, conformance=conformance,
        )
        if len(cls._json(bundle.as_dict()).encode("utf-8")) > limits.max_bytes:
            raise CatalogLimitError("generated catalogs exceed max_bytes")
        return bundle

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


__all__ = ["CatalogBundle", "CatalogLimitError", "CatalogLimits", "GeneratedCatalogs"]
