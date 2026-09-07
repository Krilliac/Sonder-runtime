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

    Some legacy memory-store operations still self-commit, so the default path
    preserves their existing behavior.  An explicitly injected authoritative
    fact source is different: this unit opens an outer SQLite transaction
    before exposing its repository, so the source's nested savepoint remains
    rollbackable with the unit of work.  Other legacy operations are not
    converted by that opt-in source boundary and can still self-commit, so
    callers that need this guarantee keep the initial fact-only path separate.
    """

    def __init__(
        self,
        db_path: str | None = None,
        *,
        authoritative_fact_source=None,
    ) -> None:
        self._db_path = db_path
        self._authoritative_fact_source = authoritative_fact_source
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
        try:
            self.memory = MemoryRepositoryAdapter(
                self._conn,
                authoritative_fact_source=self._authoritative_fact_source,
            )
            if self._authoritative_fact_source is not None:
                # SQLite's ``in_transaction`` is then true before a supported
                # source write.  SQLiteAuthoritativeFactSource uses a
                # savepoint in that case and never commits the UoW's outer
                # boundary itself.
                self._conn.execute("BEGIN IMMEDIATE")
        except BaseException:
            try:
                self._conn.rollback()
            finally:
                self._conn.close()
                self._conn = None
                self.memory = None
            raise
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
