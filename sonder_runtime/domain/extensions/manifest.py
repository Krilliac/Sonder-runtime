"""Declarative, typed contracts for extensions crossing the runtime boundary."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from enum import StrEnum
import re


_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_.-]{1,127}$")
_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")

# The wire/persistence shape produced by ``ExtensionManifest.to_dict`` and
# accepted by ``from_dict``. Bumped only when that shape changes in a way a
# reader must branch on; ``digest()`` deliberately excludes it so evolving the
# wire representation never invalidates a provenance record signed against the
# manifest's content. A dict missing this key is schema "1" by definition --
# every manifest persisted before this field existed.
MANIFEST_SCHEMA_VERSION = "1"


class HealthMode(StrEnum):
    PASSIVE = "passive"
    PROBE = "probe"
    REQUIRED = "required"


@dataclass(frozen=True)
class ExtensionIdentity:
    name: str
    publisher: str

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.name) or not _IDENTIFIER.fullmatch(self.publisher):
            raise ValueError("extension name and publisher must be bounded identifiers")


@dataclass(frozen=True)
class ExtensionDependency:
    name: str
    version: str = "*"
    required: bool = True

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.name):
            raise ValueError("dependency name must be a bounded identifier")
        if self.version != "*" and not _VERSION.fullmatch(self.version):
            raise ValueError("dependency version must be semantic or '*'")


@dataclass(frozen=True)
class ExtensionHealth:
    mode: HealthMode = HealthMode.PROBE
    crash_limit: int = 3
    probe_timeout_ms: int = 1000

    def __post_init__(self) -> None:
        if self.crash_limit < 1 or self.crash_limit > 100:
            raise ValueError("crash limit must be between 1 and 100")
        if self.probe_timeout_ms < 1 or self.probe_timeout_ms > 120_000:
            raise ValueError("probe timeout is out of bounds")


@dataclass(frozen=True)
class CleanupPolicy:
    on_quarantine: str = "disable"
    retain_state: bool = True

    def __post_init__(self) -> None:
        if self.on_quarantine not in {"disable", "unload", "remove"}:
            raise ValueError("cleanup action must be disable, unload, or remove")


@dataclass(frozen=True)
class ExtensionResources:
    """Native resource budgets declared by an extension installation."""

    memory_limit_bytes: int | None = None

    def __post_init__(self) -> None:
        if self.memory_limit_bytes is not None and (
            isinstance(self.memory_limit_bytes, bool)
            or not isinstance(self.memory_limit_bytes, int)
            or self.memory_limit_bytes <= 0
        ):
            raise ValueError("extension memory_limit_bytes must be a positive integer when set")


@dataclass(frozen=True)
class ExtensionManifest:
    identity: ExtensionIdentity
    version: str
    protocol: str
    dependencies: tuple[ExtensionDependency, ...] = ()
    permissions: tuple[str, ...] = ()
    health: ExtensionHealth = ExtensionHealth()
    cleanup: CleanupPolicy = CleanupPolicy()
    resources: ExtensionResources = ExtensionResources()

    def __post_init__(self) -> None:
        if not _VERSION.fullmatch(self.version):
            raise ValueError("extension version must be semantic")
        if not _IDENTIFIER.fullmatch(self.protocol):
            raise ValueError("protocol must be a bounded identifier")
        if len(self.dependencies) > 64 or len(self.permissions) > 64:
            raise ValueError("manifest dependency and permission lists are bounded")
        dependency_names = [item.name for item in self.dependencies]
        if len(set(dependency_names)) != len(dependency_names):
            raise ValueError("manifest dependencies must be unique")
        if self.extension_id in dependency_names:
            raise ValueError("manifest cannot depend on itself")
        if len(set(self.permissions)) != len(self.permissions) or any(not item for item in self.permissions):
            raise ValueError("permissions must be non-empty and unique")
        if any(not _IDENTIFIER.fullmatch(item) for item in self.permissions):
            raise ValueError("permissions must be bounded identifiers")
        if not isinstance(self.resources, ExtensionResources):
            raise TypeError("resources must be ExtensionResources")

    @property
    def extension_id(self) -> str:
        return f"{self.identity.publisher}.{self.identity.name}"

    def digest(self) -> str:
        """Return the stable digest bound by extension provenance records."""
        material = {
            "extension_id": self.extension_id,
            "version": self.version,
            "protocol": self.protocol,
            "dependencies": [
                {"name": item.name, "version": item.version, "required": item.required}
                for item in sorted(self.dependencies, key=lambda dependency: (dependency.name, dependency.version, dependency.required))
            ],
            "permissions": sorted(self.permissions),
            "health": {
                "mode": self.health.mode.value,
                "crash_limit": self.health.crash_limit,
                "probe_timeout_ms": self.health.probe_timeout_ms,
            },
            "cleanup": {
                "on_quarantine": self.cleanup.on_quarantine,
                "retain_state": self.cleanup.retain_state,
            },
            "resources": {
                "memory_limit_bytes": self.resources.memory_limit_bytes,
            },
        }
        encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return sha256(encoded).hexdigest()

    def compatibility_reasons(
        self, *, protocol: str, available_dependencies: set[str], granted_permissions: set[str]
    ) -> tuple[str, ...]:
        reasons: list[str] = []
        if self.protocol != protocol:
            reasons.append("protocol-incompatible")
        missing = sorted(
            dependency.name for dependency in self.dependencies
            if dependency.required and dependency.name not in available_dependencies
        )
        if missing:
            reasons.append("missing-dependency:" + ",".join(missing))
        missing_permissions = sorted(set(self.permissions) - granted_permissions)
        if missing_permissions:
            reasons.append("permission-denied:" + ",".join(missing_permissions))
        return tuple(reasons)

    def is_compatible(self, **kwargs: object) -> bool:
        return not self.compatibility_reasons(**kwargs)  # type: ignore[arg-type]

    @classmethod
    def from_plugin_manifest(cls, plugin: object, *, publisher: str = "legacy") -> "ExtensionManifest":
        return cls(
            identity=ExtensionIdentity(name=plugin.name, publisher=publisher),  # type: ignore[attr-defined]
            version=plugin.version,  # type: ignore[attr-defined]
            protocol="plugin-lite",
            dependencies=tuple(ExtensionDependency(name=item) for item in plugin.dependencies),  # type: ignore[attr-defined]
            permissions=tuple(plugin.permissions),  # type: ignore[attr-defined]
        )

    def to_dict(self) -> dict:
        """Return the canonical, versioned wire/persistence representation.

        This is the one public serialization of ``ExtensionManifest``; adapters
        (persistence, MCP/HTTP capability discovery) must use it rather than
        hand-rolling the field shape, so every surface stays in lockstep when
        the manifest gains a field.
        """
        return {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "identity": {"name": self.identity.name, "publisher": self.identity.publisher},
            "version": self.version,
            "protocol": self.protocol,
            "dependencies": [
                {"name": item.name, "version": item.version, "required": item.required}
                for item in self.dependencies
            ],
            "permissions": list(self.permissions),
            "health": {
                "mode": self.health.mode.value,
                "crash_limit": self.health.crash_limit,
                "probe_timeout_ms": self.health.probe_timeout_ms,
            },
            "cleanup": {
                "on_quarantine": self.cleanup.on_quarantine,
                "retain_state": self.cleanup.retain_state,
            },
            "resources": {"memory_limit_bytes": self.resources.memory_limit_bytes},
        }

    @classmethod
    def from_dict(cls, value: dict) -> "ExtensionManifest":
        """Rehydrate a manifest from :meth:`to_dict`'s shape.

        Unknown extra keys (a newer schema_version's additions) are ignored
        rather than rejected, so an older reader can still load a manifest
        written by a newer one; missing required keys raise ``KeyError``/
        ``TypeError`` at this boundary rather than deeper inside the dataclass.
        """
        if not isinstance(value, dict):
            raise TypeError("manifest dict must be an object")
        identity = value["identity"]
        health = value.get("health", {})
        cleanup = value.get("cleanup", {})
        resources = value.get("resources", {})
        return cls(
            identity=ExtensionIdentity(identity["name"], identity["publisher"]),
            version=value["version"],
            protocol=value["protocol"],
            dependencies=tuple(
                ExtensionDependency(item["name"], item.get("version", "*"), item.get("required", True))
                for item in value.get("dependencies", ())
            ),
            permissions=tuple(value.get("permissions", ())),
            health=ExtensionHealth(
                mode=HealthMode(health["mode"]) if "mode" in health else HealthMode.PROBE,
                crash_limit=health.get("crash_limit", 3),
                probe_timeout_ms=health.get("probe_timeout_ms", 1000),
            ),
            cleanup=CleanupPolicy(
                on_quarantine=cleanup.get("on_quarantine", "disable"),
                retain_state=cleanup.get("retain_state", True),
            ),
            resources=ExtensionResources(resources.get("memory_limit_bytes")),
        )
