"""Provider-neutral ports for the typed update application boundary."""
from __future__ import annotations

from typing import Protocol

from ..updates.bounded_state import UpdatePort, UpdateTarget


class UpdateAuthority(Protocol):
    """Explicit release authority; returning false must deny activation."""

    def authorize(self, target: UpdateTarget) -> bool: ...


class UpdateBackup(Protocol):
    """Backup/restore route used before and after activation attempts."""

    def create(self, target: UpdateTarget) -> str: ...
    def restore(self, backup_id: str) -> None: ...
    def verify(self, backup_id: str) -> bool: ...


__all__ = ["UpdateAuthority", "UpdateBackup", "UpdatePort"]
