"""Backup and restore use cases independent of storage implementation."""
from __future__ import annotations

from ..ports.backup import BackupGateway, BackupPath, BackupResultView


class BackupService:
    def __init__(self, gateway: BackupGateway) -> None:
        self._gateway = gateway

    def create(self, target: BackupPath) -> BackupResultView:
        return self._gateway.create(target)

    def verify(self, backup_dir: BackupPath) -> list[str]:
        return self._gateway.verify(backup_dir)

    def list(self, target: BackupPath) -> list[dict]:
        return self._gateway.list(target)

    def prune(self, target: BackupPath, *, keep: int) -> list[str]:
        return self._gateway.prune(target, keep=keep)

    def prune_tiered(
        self,
        target: BackupPath,
        *,
        daily: int,
        weekly: int,
        monthly: int,
    ) -> list[str]:
        return self._gateway.prune_tiered(
            target,
            daily=daily,
            weekly=weekly,
            monthly=monthly,
        )

    def smoke_restore(self, backup_dir: BackupPath) -> list[str]:
        return self._gateway.smoke_restore(backup_dir)

    def restore_to_empty(
        self, backup_dir: BackupPath, destination: BackupPath
    ) -> list[str]:
        return self._gateway.restore_to_empty(backup_dir, destination)
