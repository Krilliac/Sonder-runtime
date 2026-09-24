"""Compatibility import for the canonical filesystem lock adapter."""

from sonder_runtime.adapters.filesystem.durable_locks import (
    LockTimeout,
    exclusive_descriptor_lock,
    exclusive_file_lock,
    owner_path,
    read_owner,
)

__all__ = ["LockTimeout", "exclusive_descriptor_lock", "exclusive_file_lock", "owner_path", "read_owner"]
