"""Typed port for verified backup and restore operations."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol


BackupPath = str | os.PathLike[str]


class BackupResultView(Protocol):
    backup_id: str
    path: Path
    total_bytes: int
    file_count: int


class BackupGateway(Protocol):
    def create(self, target: BackupPath) -> BackupResultView: ...

    def verify(self, backup_dir: BackupPath) -> list[str]: ...

    def list(self, target: BackupPath) -> list[dict]: ...

    def prune(self, target: BackupPath, *, keep: int) -> list[str]: ...

    def prune_tiered(
        self,
        target: BackupPath,
        *,
        daily: int,
        weekly: int,
        monthly: int,
    ) -> list[str]: ...

    def smoke_restore(self, backup_dir: BackupPath) -> list[str]: ...

    def restore_to_empty(
        self, backup_dir: BackupPath, destination: BackupPath
    ) -> list[str]: ...
