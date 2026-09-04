"""Backup gateway adapter."""
from __future__ import annotations

import importlib
import logging

logger = logging.getLogger(__name__)


class LegacyBackupGateway:
    @staticmethod
    def _implementation():
        try:
            return importlib.import_module("sonder_runtime.adapters.backup")
        except ImportError:
            logger.warning(
                "backup adapter implementation module not found — "
                "backup operations will fail until sonder_runtime.adapters.backup is available",
                exc_info=True,
            )
            raise

    def create(self, target):
        logger.debug(f"backup create target={target!r}")
        logger.info(f"backup started for target={target!r}")
        try:
            result = self._implementation().create_backup(target)
        except Exception:
            logger.error(f"backup create failed for target={target!r}", exc_info=True)
            raise
        logger.info(f"backup completed for target={target!r}")
        return result

    def verify(self, backup_dir):
        logger.debug(f"backup verify dir={backup_dir!r}")
        logger.info(f"backup verification started for dir={backup_dir!r}")
        try:
            result = self._implementation().verify_backup(backup_dir)
        except Exception:
            logger.critical(
                f"backup verification failed for dir={backup_dir!r} — "
                f"backup may be corrupt, disaster recovery capability is compromised",
                exc_info=True,
            )
            raise
        logger.info(f"backup verification completed for dir={backup_dir!r}")
        return result

    def list(self, target):
        logger.debug(f"backup list target={target!r}")
        return self._implementation().list_backups(target)

    def prune(self, target, *, keep):
        logger.debug(f"backup prune target={target!r} keep={keep}")
        logger.info(f"backup prune started target={target!r}, keep={keep}")
        result = self._implementation().prune_backups(target, keep=keep)
        logger.info(f"backup prune completed target={target!r}")
        return result

    def prune_tiered(self, target, *, daily, weekly, monthly):
        logger.debug(f"backup prune_tiered target={target!r} daily={daily} weekly={weekly} monthly={monthly}")
        logger.info(f"backup tiered prune started target={target!r}, daily={daily}, weekly={weekly}, monthly={monthly}")
        result = self._implementation().prune_backups_tiered(
            target, daily=daily, weekly=weekly, monthly=monthly
        )
        logger.info(f"backup tiered prune completed target={target!r}")
        return result

    def smoke_restore(self, backup_dir):
        logger.debug(f"backup smoke_restore dir={backup_dir!r}")
        logger.info(f"backup smoke restore started dir={backup_dir!r}")
        try:
            result = self._implementation().restore_smoke(backup_dir)
        except Exception:
            logger.error(f"backup smoke restore failed for dir={backup_dir!r}", exc_info=True)
            raise
        logger.info(f"backup smoke restore completed dir={backup_dir!r}")
        return result

    def restore_to_empty(self, backup_dir, destination):
        logger.debug(f"backup restore_to_empty dir={backup_dir!r} destination={destination!r}")
        logger.info(f"backup full restore started dir={backup_dir!r}, destination={destination!r}")
        try:
            result = self._implementation().restore_to_empty(backup_dir, destination)
        except Exception:
            logger.critical(
                f"backup full restore failed for dir={backup_dir!r} destination={destination!r} — "
                f"disaster recovery operation did not complete, data may be unrecoverable",
                exc_info=True,
            )
            raise
        logger.info(f"backup full restore completed dir={backup_dir!r}, destination={destination!r}")
        return result
