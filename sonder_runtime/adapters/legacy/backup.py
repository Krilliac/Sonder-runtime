"""Legacy binding for the migrated backup implementation."""
from __future__ import annotations

import importlib


class LegacyBackupGateway:
    @staticmethod
    def _implementation():
        return importlib.import_module("sonder_runtime.adapters.backup")

    def create(self, target):
        return self._implementation().create_backup(target)

    def verify(self, backup_dir):
        return self._implementation().verify_backup(backup_dir)

    def list(self, target):
        return self._implementation().list_backups(target)

    def prune(self, target, *, keep):
        return self._implementation().prune_backups(target, keep=keep)

    def prune_tiered(self, target, *, daily, weekly, monthly):
        return self._implementation().prune_backups_tiered(
            target, daily=daily, weekly=weekly, monthly=monthly
        )

    def smoke_restore(self, backup_dir):
        return self._implementation().restore_smoke(backup_dir)

    def restore_to_empty(self, backup_dir, destination):
        return self._implementation().restore_to_empty(backup_dir, destination)
