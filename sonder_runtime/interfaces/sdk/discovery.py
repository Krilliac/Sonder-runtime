"""Typed capability discovery derived from the runtime's generated catalogs."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ...application.ports.tool_registry import ToolCall, ToolDescriptor, validate_tool_call
from ...application.protocol.mcp_compatibility import SUPPORTED_MCP_PROTOCOL_VERSIONS
from ...application.tools.generated_catalogs import CatalogBundle
from .contracts import (
    SDK_PROTOCOL_VERSION,
    SUPPORTED_SDK_PROTOCOL_VERSIONS,
    SdkContractError,
    SdkDiagnostic,
)


DISCOVERY_SCHEMA = "sonder-sdk-discovery-v1"


@dataclass(frozen=True, slots=True)
class ToolCapability:
    name: str
    description: str
    input_schema: Mapping[str, Any]
    effects: tuple[str, ...] = ()
    execution_class: str = "pure"

    def __post_init__(self) -> None:
        # Reuse the runtime's canonical descriptor validation, so SDK clients
        # validate the same schema shape as the permission-gated gateway.
        if not isinstance(self.name, str) or not isinstance(self.description, str):
            raise SdkContractError("tool capability name and description must be text")
        if not isinstance(self.input_schema, Mapping):
            raise SdkContractError("tool capability input_schema must be an object")
        ToolDescriptor(self.name, self.description, dict(self.input_schema))
        if (
            not isinstance(self.effects, tuple)
            or any(not isinstance(item, str) or not item for item in self.effects)
            or len(set(self.effects)) != len(self.effects)
        ):
            raise SdkContractError("tool capability effects must be unique non-empty strings")
        if not isinstance(self.execution_class, str) or not self.execution_class:
            raise SdkContractError("tool capability execution_class is required")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ToolCapability":
        if not isinstance(value, Mapping):
            raise SdkContractError("tool capability must be an object")
        unexpected = sorted(set(value) - {"description", "effects", "execution_class", "input_schema", "name"})
        if unexpected:
            raise SdkContractError(f"tool capability contains unknown field(s): {', '.join(unexpected)}")
        schema = value.get("input_schema", {})
        effects = value.get("effects", ())
        if not isinstance(schema, Mapping) or not isinstance(effects, (list, tuple)):
            raise SdkContractError("tool capability schema/effects fields are invalid")
        return cls(
            name=value.get("name", ""),
            description=value.get("description", ""),
            input_schema=dict(schema),
            effects=tuple(effects),
            execution_class=value.get("execution_class", ""),
        )

    def validate_arguments(self, arguments: Mapping[str, Any]) -> None:
        if not isinstance(arguments, Mapping):
            raise SdkContractError("tool arguments must be an object")
        descriptor = ToolDescriptor(self.name, self.description, dict(self.input_schema))
        validate_tool_call(descriptor, ToolCall(self.name, dict(arguments)))

    def as_dict(self) -> dict[str, Any]:
        return {
            "description": self.description,
            "effects": list(self.effects),
            "execution_class": self.execution_class,
            "input_schema": dict(self.input_schema),
            "name": self.name,
        }


@dataclass(frozen=True, slots=True)
class CapabilitySnapshot:
    """A discovery snapshot; permission metadata is descriptive, never a grant."""

    catalog_digest: str
    tools: tuple[ToolCapability, ...]
    runtime_version: str
    sdk_versions: tuple[str, ...] = SUPPORTED_SDK_PROTOCOL_VERSIONS
    mcp_versions: tuple[str, ...] = SUPPORTED_MCP_PROTOCOL_VERSIONS
    schema: str = DISCOVERY_SCHEMA
    authorization: str = "runtime-evaluated"

    def __post_init__(self) -> None:
        if self.schema != DISCOVERY_SCHEMA or self.authorization != "runtime-evaluated":
            raise SdkContractError("unsupported SDK discovery contract")
        if not isinstance(self.catalog_digest, str) or len(self.catalog_digest) != 64 or any(ch not in "0123456789abcdefABCDEF" for ch in self.catalog_digest):
            raise SdkContractError("catalog_digest must be a SHA-256 hex digest")
        if not isinstance(self.runtime_version, str) or not self.runtime_version or not self.sdk_versions or not self.mcp_versions:
            raise SdkContractError("discovery versions are required")
        if any(not isinstance(version, str) or not version for version in (*self.sdk_versions, *self.mcp_versions)):
            raise SdkContractError("discovery versions must be non-empty strings")
        if any(version not in SUPPORTED_SDK_PROTOCOL_VERSIONS for version in self.sdk_versions):
            raise SdkContractError("discovery advertises an unsupported SDK version")
        names = [tool.name for tool in self.tools]
        if names != sorted(names) or len(set(names)) != len(names):
            raise SdkContractError("discovered tools must be unique and sorted")

    @classmethod
    def from_catalogs(
        cls, catalogs: CatalogBundle, *, runtime_version: str
    ) -> "CapabilitySnapshot":
        if not isinstance(catalogs, CatalogBundle):
            raise TypeError("catalogs must be a CatalogBundle")
        raw_tools = catalogs.client.get("tools", ())
        if not isinstance(raw_tools, Sequence) or isinstance(raw_tools, (str, bytes)):
            raise SdkContractError("generated client tool catalog must be an array")
        tools = tuple(ToolCapability.from_dict(item) for item in raw_tools)
        client_names = tuple(tool.name for tool in tools)
        mcp_rows = catalogs.mcp.get("tools", ())
        if not isinstance(mcp_rows, Sequence) or isinstance(mcp_rows, (str, bytes)):
            raise SdkContractError("generated MCP tool catalog must be an array")
        mcp_names = tuple(sorted(str(row.get("name", "")) for row in mcp_rows if isinstance(row, Mapping)))
        if tuple(sorted(client_names)) != mcp_names:
            raise SdkContractError("generated MCP and client tool catalogs disagree")
        return cls(catalogs.digest.lower(), tuple(sorted(tools, key=lambda item: item.name)), runtime_version)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CapabilitySnapshot":
        if not isinstance(value, Mapping):
            raise SdkContractError("SDK discovery response must be an object")
        allowed = {"authorization", "catalog_digest", "mcp_versions", "runtime_version", "schema", "sdk_versions", "tools"}
        unexpected = sorted(set(value) - allowed)
        if unexpected:
            raise SdkContractError(f"SDK discovery contains unknown field(s): {', '.join(unexpected)}")
        raw_tools = value.get("tools", ())
        raw_sdk_versions = value.get("sdk_versions", ())
        raw_mcp_versions = value.get("mcp_versions", ())
        if not all(
            isinstance(item, list)
            for item in (raw_tools, raw_sdk_versions, raw_mcp_versions)
        ):
            raise SdkContractError("SDK discovery tools and versions must be arrays")
        return cls(
            catalog_digest=value.get("catalog_digest", ""),
            tools=tuple(ToolCapability.from_dict(item) for item in raw_tools),
            runtime_version=value.get("runtime_version", ""),
            sdk_versions=tuple(raw_sdk_versions),
            mcp_versions=tuple(raw_mcp_versions),
            schema=value.get("schema", ""),
            authorization=value.get("authorization", ""),
        )

    def negotiate(self, client_versions: Sequence[str]) -> str:
        agreed = next((version for version in self.sdk_versions if version in client_versions), None)
        if agreed is None:
            raise SdkContractError("no supported SDK protocol version")
        return agreed

    def require_tool(self, name: str) -> ToolCapability:
        tool = next((item for item in self.tools if item.name == name), None)
        if tool is None:
            raise SdkContractError(f"unknown SDK tool {name!r}")
        return tool

    def diagnostics(self) -> tuple[SdkDiagnostic, ...]:
        """Return actionable diagnostics without probing or changing the host."""
        issues = []
        if SDK_PROTOCOL_VERSION not in self.sdk_versions:
            issues.append(SdkDiagnostic("SDK_VERSION_MISMATCH", "client protocol is not advertised", "sdk_versions"))
        if not self.tools:
            issues.append(SdkDiagnostic("SDK_EMPTY_CATALOG", "runtime advertised no tools", "tools"))
        return tuple(issues)

    def as_dict(self) -> dict[str, Any]:
        return {
            "authorization": self.authorization,
            "catalog_digest": self.catalog_digest,
            "mcp_versions": list(self.mcp_versions),
            "runtime_version": self.runtime_version,
            "schema": self.schema,
            "sdk_versions": list(self.sdk_versions),
            "tools": [tool.as_dict() for tool in self.tools],
        }


__all__ = ["CapabilitySnapshot", "DISCOVERY_SCHEMA", "ToolCapability"]
