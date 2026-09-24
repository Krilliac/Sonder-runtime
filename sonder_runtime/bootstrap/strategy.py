"""Host-owned composition for observe-only strategy checkpoints.

The seal key lives at a stable state path, separate from the SQLite database.
POSIX ownership and mode are checked. Windows relies on the host state ACL;
POSIX mode bits alone cannot establish equivalent Windows key privacy. A
missing key is created once only for a new database; a lost, malformed or
insecure key is never regenerated because that would orphan sealed checkpoints.
This repository has no effect journal binding, so observations cannot authorize
effect replay or claim that an external effect has been reconciled.
"""
from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

from sonder_runtime.adapters.persistence.sqlite.runtime_checkpoints import (
    SQLiteRuntimeCheckpointRepository,
)
from sonder_runtime.application.ports.runtime_checkpoints import CheckpointError
from sonder_runtime.application.strategy.tracing import StrategyTraceService
from sonder_runtime.platform.paths import state_path

_LOG = logging.getLogger(__name__)


def _private_key(path: Path) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        directory = path.parent.lstat()
        if not stat.S_ISDIR(directory.st_mode) or directory.st_mode & 0o077:
            raise CheckpointError("strategy key directory is not private")
        if directory.st_uid != os.getuid():
            raise CheckpointError("strategy key directory has another owner")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        pass
    else:
        try:
            key = os.urandom(32)
            if os.write(descriptor, key) != len(key):
                raise CheckpointError("strategy key write was incomplete")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if os.name == "posix":
            directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    read_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, read_flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise CheckpointError("strategy key is not a private regular file")
        if os.name == "posix" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise CheckpointError("strategy key has insecure ownership or permissions")
        key = os.read(descriptor, 33)
        if len(key) != 32:
            raise CheckpointError("strategy key is missing or malformed")
        return key
    finally:
        os.close(descriptor)


def compose_strategy_trace(*, db_path: str | Path | None = None,
                           key_path: str | Path | None = None) -> StrategyTraceService:
    """Compose the sealed observer using host paths and no replay authorization."""
    database = Path(db_path or state_path("strategy/checkpoints.db", "SONDER_STRATEGY_CHECKPOINT_DB"))
    key_file = Path(key_path or state_path("strategy-private/checkpoint.key"))
    if database.resolve() == key_file.resolve():
        raise CheckpointError("strategy key and database paths must be separate")
    if database.exists() and not key_file.exists():
        raise CheckpointError("strategy checkpoint database exists without its seal key")
    key = _private_key(key_file)
    repository = SQLiteRuntimeCheckpointRepository(database, seal_key=key)
    return StrategyTraceService(repository)


def configured_strategy_trace() -> StrategyTraceService | None:
    """Read the rollout switch only at the host composition boundary."""
    value = os.environ.get("SONDER_STRATEGY_OBSERVE", "").strip().lower()
    if value in {"", "0", "false", "off"}:
        return None
    if value not in {"1", "true", "on"}:
        raise ValueError("SONDER_STRATEGY_OBSERVE must be true or false")
    return compose_strategy_trace()


def try_configured_strategy_trace() -> StrategyTraceService | None:
    """Keep optional observation faults from changing work execution."""
    try:
        return configured_strategy_trace()
    except Exception as error:  # noqa: BLE001 - optional observation never gates work
        _LOG.warning("strategy observation unavailable: %s", type(error).__name__)
        return None


def try_compose_strategy_memory(trace: StrategyTraceService | None, unit_of_work_provider):
    """Bind optional strategy memory to the canonical application unit of work."""
    if trace is None:
        return None
    try:
        from sonder_runtime.application.memory.strategy_memory import (
            StrategyMemoryService,
        )

        unit_of_work = unit_of_work_provider()
        if not callable(unit_of_work):
            raise TypeError("application unit_of_work must be a factory")
        return StrategyMemoryService(trace, unit_of_work)
    except Exception as error:  # noqa: BLE001 - optional indexing never gates work
        _LOG.warning("strategy memory observation unavailable: %s", type(error).__name__)
        return None


__all__ = ["compose_strategy_trace", "configured_strategy_trace", "try_compose_strategy_memory",
           "try_configured_strategy_trace"]
