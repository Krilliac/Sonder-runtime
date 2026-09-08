"""Explicit child aggregate backend selection; credentials stay external."""

from dataclasses import dataclass, field, replace
from pathlib import Path
import re


@dataclass(frozen=True)
class ChildStorageConfig:
    backend: str = "sqlite"
    binding_file: str = field(default="", repr=False)
    owner_id: str = ""
    durability: str = "primary"
    required_standby: str = ""
    pool_size: int = 2
    operation_timeout_seconds: int = 5
    cancel_timeout_seconds: int = 1


def child_storage_errors(config):
    section = config.child_storage
    errors = []
    if section.backend not in ("sqlite", "postgresql"):
        errors.append("[child_storage].backend must be sqlite or postgresql")
    if section.durability not in ("primary", "sync-pair"):
        errors.append("[child_storage].durability must be primary or sync-pair")
    for name, low, high in (
        ("pool_size", 1, 4),
        ("operation_timeout_seconds", 1, 30),
        ("cancel_timeout_seconds", 1, 5),
    ):
        value = getattr(section, name)
        if type(value) is not int or not low <= value <= high:
            errors.append(f"[child_storage].{name} must be an integer in {low}..{high}")
    for name in ("owner_id", "required_standby"):
        value = getattr(section, name)
        if not isinstance(value, str) or (
            value and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value)
        ):
            errors.append(f"[child_storage].{name} must be a bounded identifier")
    path = section.binding_file
    if (
        not isinstance(path, str)
        or len(path) > 4096
        or (
            path
            and (not Path(path).is_absolute() or any(ord(char) < 32 for char in path))
        )
    ):
        errors.append("[child_storage].binding_file must be an absolute private path")
    if section.backend == "postgresql":
        if not section.owner_id or not section.binding_file:
            errors.append(
                "[child_storage] PostgreSQL requires owner_id and private binding_file"
            )
        if section.durability == "sync-pair" and not section.required_standby:
            errors.append("[child_storage] sync-pair requires required_standby")
    return errors


def apply_child_storage_environment(section, env, errors):
    values = {}
    for name in (
        "backend",
        "binding_file",
        "owner_id",
        "durability",
        "required_standby",
        "pool_size",
        "operation_timeout_seconds",
        "cancel_timeout_seconds",
    ):
        variable = "SONDER_CHILD_STORAGE_" + name.upper()
        if variable not in env:
            continue
        value = env[variable].strip()
        if name in ("pool_size", "operation_timeout_seconds", "cancel_timeout_seconds"):
            try:
                value = int(value)
            except ValueError:
                errors.append(variable + " must be an integer")
                continue
        values[name] = value
    section = replace(section, **values)
    if (
        section.backend == "postgresql"
        and env.get("SONDER_CHILD_SESSIONS_DB", "").strip()
    ):
        errors.append(
            "PostgreSQL child storage conflicts with SONDER_CHILD_SESSIONS_DB"
        )
    return section
