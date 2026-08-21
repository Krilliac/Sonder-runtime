"""Canonical UnitOfWork adapter for the memory-backed application graph."""
from __future__ import annotations

from .memory_repository import MemoryRepositoryAdapter
from .operations_event_sink import OperationsEventSink
from .persistence.autopilot_repository import AutopilotRepository
from .runtime_policy_repository import RuntimePolicyRepository


class UnitOfWorkAdapter:
    """Own one memory-store connection for a transaction scope.

    The automation and policy repositories are connection-independent and the
    operations event sink owns its own store.  The memory repository is bound
    to the connection opened when the scope is entered.

    Some legacy memory-store operations still self-commit, so rollback does
    not yet undo those operations.  This adapter owns the connection lifecycle
    today; the transaction boundary tightens as those operations migrate off
    self-commit.
    """

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path
        self._conn = None
        self.memory = None
        self.automation = AutopilotRepository()
        self.policy = RuntimePolicyRepository()
        self.events = OperationsEventSink()

    def __enter__(self) -> "UnitOfWorkAdapter":
        import sonder_runtime.adapters.memory_store as memory_store
        from sonder_runtime.platform import paths

        path = self._db_path or paths.memory_db_path()
        self._conn = memory_store.connect(path)
        self.memory = MemoryRepositoryAdapter(self._conn)
        return self

    @property
    def connection(self):
        """Expose the caller-owned connection only to application ports."""
        if self._conn is None:
            raise RuntimeError("unit of work is not active")
        return self._conn

    def commit(self) -> None:
        if self._conn is not None:
            self._conn.commit()

    def rollback(self) -> None:
        if self._conn is not None:
            self._conn.rollback()

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                self.commit()
            else:
                self.rollback()
        finally:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
                self.memory = None
