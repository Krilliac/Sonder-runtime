"""Strict, declarative plugin manifests for SDK consumers."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping

from ...application.extensions.facade import build_extension_manifest
from .contracts import SdkContractError, SdkDiagnostic


PLUGIN_MANIFEST_SCHEMA_VERSION = "1"
_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:[.+-][0-9A-Za-z.-]+)?$")
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_.-]{1,127}$")
_PLUGIN_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_-]{1,127}$")

PLUGIN_MANIFEST_JSON_SCHEMA: Mapping[str, Any] = {
    "$id": "https://sonder.runtime/schemas/sdk-plugin-manifest-v1.json",
    "type": "object",
    "additionalProperties": False,
    "required": ["capabilities", "name", "publisher", "schema_version", "version"],
    "properties": {
        "schema_version": {"const": PLUGIN_MANIFEST_SCHEMA_VERSION},
        "name": {"type": "string", "pattern": _PLUGIN_IDENTIFIER.pattern},
        "publisher": {"type": "string", "pattern": _PLUGIN_IDENTIFIER.pattern},
        "version": {"type": "string", "pattern": _VERSION.pattern},
        "minimum_runtime": {"type": "string", "pattern": _VERSION.pattern},
        "maximum_runtime_exclusive": {"type": ["string", "null"], "pattern": _VERSION.pattern},
        "capabilities": {"type": "array", "items": {"type": "string", "pattern": _IDENTIFIER.pattern}, "uniqueItems": True},
        "permissions": {"type": "array", "items": {"type": "string", "pattern": _IDENTIFIER.pattern}, "uniqueItems": True},
        "dependencies": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name"],
                "properties": {
                    "name": {"type": "string", "pattern": _IDENTIFIER.pattern},
                    "version": {"type": "string"},
                    "required": {"type": "boolean"},
                },
            },
        },
    },
}


def _version(value: str, label: str) -> tuple[int, int, int]:
    match = _VERSION.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise SdkContractError(f"{label} must be a semantic version")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class PluginDependency:
    name: str
    version: str = "*"
    required: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _IDENTIFIER.fullmatch(self.name):
            raise SdkContractError("dependency name must be a bounded identifier")
        if self.version != "*":
            _version(self.version, "dependency version")
        if not isinstance(self.required, bool):
            raise SdkContractError("dependency required must be boolean")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PluginDependency":
        if not isinstance(value, Mapping):
            raise SdkContractError("plugin dependency must be an object")
        unexpected = sorted(set(value) - {"name", "required", "version"})
        if unexpected:
            raise SdkContractError(f"plugin dependency contains unknown field(s): {', '.join(unexpected)}")
        return cls(value.get("name", ""), value.get("version", "*"), value.get("required", True))

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "required": self.required, "version": self.version}


@dataclass(frozen=True, slots=True)
class PluginCompatibility:
    issues: tuple[SdkDiagnostic, ...] = ()

    @property
    def compatible(self) -> bool:
        return not self.issues


@dataclass(frozen=True, slots=True)
class SdkPluginManifest:
    """A versioned plugin declaration; parsing never imports plugin code."""

    name: str
    publisher: str
    version: str
    capabilities: tuple[str, ...]
    permissions: tuple[str, ...] = ()
    dependencies: tuple[PluginDependency, ...] = ()
    minimum_runtime: str = "0.9.0"
    maximum_runtime_exclusive: str | None = "1.0.0"
    schema_version: str = PLUGIN_MANIFEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != PLUGIN_MANIFEST_SCHEMA_VERSION:
            raise SdkContractError("unsupported plugin manifest schema_version")
        if (
            not isinstance(self.name, str)
            or not isinstance(self.publisher, str)
            or not _PLUGIN_IDENTIFIER.fullmatch(self.name)
            or not _PLUGIN_IDENTIFIER.fullmatch(self.publisher)
        ):
            raise SdkContractError("plugin name and publisher must be bounded identifiers")
        _version(self.version, "plugin version")
        minimum = _version(self.minimum_runtime, "minimum_runtime")
        if self.maximum_runtime_exclusive is not None:
            maximum = _version(self.maximum_runtime_exclusive, "maximum_runtime_exclusive")
            if maximum <= minimum:
                raise SdkContractError("maximum_runtime_exclusive must exceed minimum_runtime")
        if not self.capabilities or len(self.capabilities) > 64:
            raise SdkContractError("plugin must declare between 1 and 64 capabilities")
        for label, values in (("capabilities", self.capabilities), ("permissions", self.permissions)):
            if not isinstance(values, tuple) or len(values) > 64:
                raise SdkContractError(f"plugin {label} must be unique and bounded")
            if any(not isinstance(item, str) or not _IDENTIFIER.fullmatch(item) for item in values):
                raise SdkContractError(f"plugin {label} must contain bounded identifiers")
            if len(set(values)) != len(values):
                raise SdkContractError(f"plugin {label} must be unique and bounded")
        if (
            not isinstance(self.dependencies, tuple)
            or any(not isinstance(item, PluginDependency) for item in self.dependencies)
            or len(self.dependencies) > 64
            or len({item.name for item in self.dependencies}) != len(self.dependencies)
        ):
            raise SdkContractError("plugin dependencies must be unique and bounded")
        if any(item.name == self.extension_id for item in self.dependencies):
            raise SdkContractError("plugin cannot depend on itself")

    @property
    def extension_id(self) -> str:
        return f"{self.publisher}.{self.name}"

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SdkPluginManifest":
        if not isinstance(value, Mapping):
            raise SdkContractError("plugin manifest must be an object")
        allowed = {
            "capabilities", "dependencies", "maximum_runtime_exclusive", "minimum_runtime",
            "name", "permissions", "publisher", "schema_version", "version",
        }
        unexpected = sorted(set(value) - allowed)
        if unexpected:
            raise SdkContractError(f"plugin manifest contains unknown field(s): {', '.join(unexpected)}")
        try:
            raw_dependencies = value.get("dependencies", ())
            raw_capabilities = value.get("capabilities", ())
            raw_permissions = value.get("permissions", ())
            if not all(isinstance(item, (list, tuple)) for item in (raw_dependencies, raw_capabilities, raw_permissions)):
                raise SdkContractError("plugin manifest list fields must be arrays")
            dependencies = tuple(PluginDependency.from_dict(item) for item in raw_dependencies)
            return cls(
                name=value.get("name", ""), publisher=value.get("publisher", ""),
                version=value.get("version", ""), capabilities=tuple(raw_capabilities),
                permissions=tuple(raw_permissions), dependencies=dependencies,
                minimum_runtime=value.get("minimum_runtime", "0.9.0"),
                maximum_runtime_exclusive=value.get("maximum_runtime_exclusive", "1.0.0"),
                schema_version=value.get("schema_version", ""),
            )
        except TypeError as exc:
            raise SdkContractError("plugin manifest arrays have invalid values") from exc

    def compatibility(
        self,
        *,
        runtime_version: str,
        granted_permissions: set[str],
        available_dependencies: set[str] | None = None,
        supported_capabilities: set[str] | None = None,
    ) -> PluginCompatibility:
        if not isinstance(granted_permissions, set):
            raise SdkContractError("granted_permissions must be a set")
        if available_dependencies is not None and not isinstance(available_dependencies, set):
            raise SdkContractError("available_dependencies must be a set when supplied")
        if supported_capabilities is not None and not isinstance(supported_capabilities, set):
            raise SdkContractError("supported_capabilities must be a set when supplied")
        issues: list[SdkDiagnostic] = []
        runtime = _version(runtime_version, "runtime_version")
        if runtime < _version(self.minimum_runtime, "minimum_runtime"):
            issues.append(SdkDiagnostic("PLUGIN_RUNTIME_TOO_OLD", "runtime is older than the plugin minimum", "minimum_runtime"))
        if self.maximum_runtime_exclusive is not None and runtime >= _version(self.maximum_runtime_exclusive, "maximum_runtime_exclusive"):
            issues.append(SdkDiagnostic("PLUGIN_RUNTIME_TOO_NEW", "runtime is outside the plugin compatibility range", "maximum_runtime_exclusive"))
        missing_permissions = sorted(set(self.permissions) - granted_permissions)
        if missing_permissions:
            issues.append(SdkDiagnostic("PLUGIN_PERMISSION_DENIED", "missing permission(s): " + ", ".join(missing_permissions), "permissions"))
        if available_dependencies is not None:
            missing_dependencies = sorted(item.name for item in self.dependencies if item.required and item.name not in available_dependencies)
            if missing_dependencies:
                issues.append(SdkDiagnostic("PLUGIN_DEPENDENCY_MISSING", "missing dependency/dependencies: " + ", ".join(missing_dependencies), "dependencies"))
        if supported_capabilities is not None:
            unsupported = sorted(set(self.capabilities) - supported_capabilities)
            if unsupported:
                issues.append(SdkDiagnostic("PLUGIN_CAPABILITY_UNSUPPORTED", "unsupported capability/capabilities: " + ", ".join(unsupported), "capabilities"))
        return PluginCompatibility(tuple(issues))

    def to_extension_manifest(self) -> Any:
        """Project into the runtime registry without granting permissions."""
        return build_extension_manifest(
            self.extension_id,
            self.version,
            "sonder-sdk-v1",
            dependencies=tuple(item.as_dict() for item in self.dependencies),
            permissions=self.permissions,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "capabilities": list(self.capabilities),
            "dependencies": [item.as_dict() for item in self.dependencies],
            "maximum_runtime_exclusive": self.maximum_runtime_exclusive,
            "minimum_runtime": self.minimum_runtime,
            "name": self.name,
            "permissions": list(self.permissions),
            "publisher": self.publisher,
            "schema_version": self.schema_version,
            "version": self.version,
        }


__all__ = [
    "PLUGIN_MANIFEST_JSON_SCHEMA", "PLUGIN_MANIFEST_SCHEMA_VERSION", "PluginCompatibility",
    "PluginDependency", "SdkPluginManifest",
]
