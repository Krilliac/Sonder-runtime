"""Explicit single-binding receiver configuration; contains no credentials."""
from dataclasses import dataclass
from pathlib import Path
import time


@dataclass(frozen=True)
class ArtifactTransferConfig:
    enabled: bool = False
    store_dir: str = ""
    principal_id: str = ""
    project_id: str = ""
    peer_node_id: str = ""
    grant_id: str = ""
    grant_revision: int = 1
    expires_at: int = 0
    can_read: bool = False
    can_write: bool = False
    max_object_bytes: int = 256 * 1024 * 1024
    quota_bytes: int = 1024 * 1024 * 1024
    total_bytes: int = 2 * 1024 * 1024 * 1024
    ttl_seconds: int = 3600


def artifact_transfer_errors(config) -> list[str]:
    section = config.artifact_transfer
    errors = []
    for name in ("enabled", "can_read", "can_write"):
        if type(getattr(section, name)) is not bool:
            errors.append(f"[artifact_transfer].{name} must be a boolean")
    for name in ("principal_id", "project_id", "peer_node_id", "grant_id"):
        value = getattr(section, name)
        if not isinstance(value, str) or len(value) > 128 or any(
            ord(char) < 33 or ord(char) == 127 for char in value
        ) or (section.enabled and not value):
            errors.append(f"[artifact_transfer].{name} must be a bounded nonempty identifier when enabled")
    bounds = {"grant_revision": (1, 2**63 - 1), "expires_at": (0, 2**53 - 1),
              "max_object_bytes": (0, 64 * 1024**3),
              "quota_bytes": (1, 128 * 1024**3), "total_bytes": (1, 128 * 1024**3),
              "ttl_seconds": (1, 86400)}
    for name, (low, high) in bounds.items():
        value = getattr(section, name)
        if type(value) is not int or not low <= value <= high:
            errors.append(f"[artifact_transfer].{name} must be an integer in {low}..{high}")
    if not isinstance(section.store_dir, str) or len(section.store_dir) > 4096 or (
        section.store_dir and (not Path(section.store_dir).is_absolute()
                              or any(ord(char) < 32 for char in section.store_dir))
    ):
        errors.append("[artifact_transfer].store_dir must be an absolute private path")
    if section.enabled:
        if type(section.expires_at) is int and section.expires_at <= time.time():
            errors.append("[artifact_transfer].expires_at must be in the future")
        if not section.can_read and not section.can_write:
            errors.append("[artifact_transfer] requires an explicit read or write grant")
        key = config.secrets.artifact_transfer_key
        if not isinstance(key, str) or not 32 <= len(key) <= 512 or any(
            ord(char) < 33 or ord(char) > 126 for char in key
        ):
            errors.append("artifact transfer requires a dedicated 32..512 character secret")
        elif key == config.secrets.api_key:
            errors.append("artifact transfer dedicated key must be distinct from the global API key")
    return errors


def private_store_path(config) -> Path:
    from sonder_runtime.platform.paths import default_home
    section = config.artifact_transfer
    if section.store_dir:
        return Path(section.store_dir)
    home = Path(config.state.home or default_home()).absolute()
    return home.with_name(home.name + "-artifact-private")
