"""Declarative plugin-lite manifests with no executable import side effects."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PluginManifest:
    name: str
    version: str
    capabilities: tuple[str, ...]
    permissions: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    compatible_runtime: str = "*"

    def __post_init__(self) -> None:
        if not self.name or not self.version or any(not item for item in self.capabilities):
            raise ValueError("plugin identity and capabilities are required")

    def is_compatible(self, runtime_version: str) -> bool:
        return self.compatible_runtime == "*" or self.compatible_runtime == runtime_version


def validate_manifest(manifest: PluginManifest, *, runtime_version: str, granted_permissions: set[str]) -> tuple[bool, tuple[str, ...]]:
    missing = tuple(permission for permission in manifest.permissions if permission not in granted_permissions)
    return manifest.is_compatible(runtime_version) and not missing, missing
