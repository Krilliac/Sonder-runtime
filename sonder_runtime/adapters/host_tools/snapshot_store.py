"""Persisted host tool inventory snapshot (JSON in the state home).

Load never raises: a missing, oversized, non-regular, symlinked, corrupt, or
schema-invalid file yields ``None`` and a warning.  Save writes a private
temporary file in the same directory and atomically replaces the target.
The stored snapshot is evidence for display only; executables it names are
re-validated by the host guard before any launch.
"""
from __future__ import annotations

import json
import logging
import os
import stat
import tempfile

from sonder_runtime.domain.common.errors import InvalidInput
from sonder_runtime.domain.host_tools.model import (
    InventorySnapshot,
    snapshot_from_wire,
    snapshot_to_wire,
)
import sonder_runtime.platform.paths as runtime_paths
import sonder_runtime.platform.private_files as private_files

_logger = logging.getLogger(__name__)

MAX_SNAPSHOT_BYTES = 1024 * 1024
DEFAULT_FILENAME = "host-tools.json"


def default_snapshot_path() -> str:
    return runtime_paths.state_path(DEFAULT_FILENAME)


class JsonSnapshotStore:
    """Implements the ``InventorySnapshotStore`` port."""

    def __init__(self, path: str) -> None:
        if not isinstance(path, str) or not path or "\x00" in path:
            raise ValueError("snapshot path must be a non-empty string")
        self._path = os.path.abspath(path)

    @property
    def path(self) -> str:
        return self._path

    def load(self) -> InventorySnapshot | None:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            if stat.S_ISLNK(os.lstat(self._path).st_mode):
                _logger.warning("host tool snapshot ignored: symlink")
                return None
            fd = os.open(self._path, flags)
        except FileNotFoundError:
            return None
        except OSError as error:
            _logger.warning("host tool snapshot unreadable: %s", type(error).__name__)
            return None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                _logger.warning("host tool snapshot ignored: not a regular file")
                return None
            if info.st_size > MAX_SNAPSHOT_BYTES:
                _logger.warning("host tool snapshot ignored: oversized")
                return None
            with os.fdopen(fd, "rb", closefd=False) as handle:
                data = handle.read(MAX_SNAPSHOT_BYTES + 1)
        except OSError as error:
            _logger.warning("host tool snapshot unreadable: %s", type(error).__name__)
            return None
        finally:
            os.close(fd)
        if len(data) > MAX_SNAPSHOT_BYTES:
            _logger.warning("host tool snapshot ignored: oversized")
            return None
        try:
            return snapshot_from_wire(json.loads(data.decode("utf-8")))
        except (ValueError, UnicodeDecodeError, InvalidInput, TypeError, RecursionError) as error:
            _logger.warning("host tool snapshot ignored: %s", type(error).__name__)
            return None

    def save(self, snapshot: InventorySnapshot) -> None:
        payload = json.dumps(
            snapshot_to_wire(snapshot), ensure_ascii=True, sort_keys=True, separators=(",", ":"),
        ).encode("ascii")
        if len(payload) > MAX_SNAPSHOT_BYTES:
            raise ValueError("host tool snapshot exceeds the size limit")
        directory = os.path.dirname(self._path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix=".host-tools.", suffix=".tmp", dir=directory)
        try:
            if private_files.supported():
                os.fchmod(fd, private_files.PRIVATE_FILE_MODE)
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self._path)
        except BaseException:
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise


__all__ = ["DEFAULT_FILENAME", "JsonSnapshotStore", "MAX_SNAPSHOT_BYTES", "default_snapshot_path"]
