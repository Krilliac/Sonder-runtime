"""Disabled-by-default private source-spool configuration."""

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path


_MAX_OBJECT_BYTES = 64 * 1024**3
_MAX_TOTAL_BYTES = 128 * 1024**3


def _identifier(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 128
        and all(33 <= ord(character) <= 126 for character in value)
    )


@dataclass(frozen=True)
class ArtifactMobilitySourceConfig:
    enabled: bool = False
    store_dir: str = field(default="", repr=False)
    principal_id: str = ""
    project_id: str = ""
    source_owner_id: str = ""
    max_object_bytes: int = 256 * 1024 * 1024
    total_bytes: int = 2 * 1024 * 1024 * 1024
    ttl_seconds: int = 24 * 60 * 60


def source_scope_id(section: ArtifactMobilitySourceConfig) -> str:
    """Return the destination-independent private source namespace identity."""
    if not isinstance(section, ArtifactMobilitySourceConfig):
        raise ValueError("INVALID_SOURCE_SCOPE")
    canonical = json.dumps(
        [section.principal_id, section.project_id, section.source_owner_id],
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def artifact_mobility_source_errors(config) -> list[str]:
    section = config.artifact_mobility_source
    errors: list[str] = []
    if type(section.enabled) is not bool:
        errors.append("[artifact_mobility_source].enabled invalid")
    for name in ("principal_id", "project_id", "source_owner_id"):
        value = getattr(section, name)
        if (section.enabled and not _identifier(value)) or (
            value and not _identifier(value)
        ):
            errors.append(f"[artifact_mobility_source].{name} invalid")
    store_dir = section.store_dir
    if (
        not isinstance(store_dir, str)
        or len(store_dir) > 4096
        or any(ord(character) < 32 for character in store_dir)
        or (section.enabled and (not store_dir or not Path(store_dir).is_absolute()))
        or (store_dir and not Path(store_dir).is_absolute())
    ):
        errors.append("[artifact_mobility_source].store_dir invalid")
    bounds = {
        "max_object_bytes": (1, _MAX_OBJECT_BYTES),
        "total_bytes": (1, _MAX_TOTAL_BYTES),
        "ttl_seconds": (1, 86400),
    }
    for name, (minimum, maximum) in bounds.items():
        value = getattr(section, name)
        if type(value) is not int or not minimum <= value <= maximum:
            errors.append(f"[artifact_mobility_source].{name} invalid")
    if (
        type(section.max_object_bytes) is int
        and type(section.total_bytes) is int
        and section.max_object_bytes > section.total_bytes
    ):
        errors.append("[artifact_mobility_source].total_bytes invalid")
    return errors
