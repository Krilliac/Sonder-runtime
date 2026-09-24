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
    fact source is different: this unit opens an outer SQLite transaction only
    when its repository attempts a supported source write, so an untouched
    unit does not reserve SQLite's writer lock.  The source then uses its
    nested savepoint and the unit of work owns the outer commit or rollback.
    Other legacy operations are not converted by that opt-in source boundary
    and can still self-commit, so callers that need this guarantee keep the
    initial fact-only path separate.
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
            if self._authoritative_fact_source is not None:
                # Publish the durable authority marker before handing the
                # connection to application callers.  This fences the legacy
                # memory-store helpers for the configured project as well.
                self._authoritative_fact_source.activate(self._conn)
            self.memory = MemoryRepositoryAdapter(
                self._conn,
                authoritative_fact_source=self._authoritative_fact_source,
                begin_authoritative_transaction=(
                    self._begin_authoritative_transaction
                    if self._authoritative_fact_source is not None
                    else None
                ),
            )
        except BaseException:
            try:
                self._conn.rollback()
            finally:
                self._conn.close()
                self._conn = None
                self.memory = None
            raise
        return self

    def _begin_authoritative_transaction(self) -> None:
        """Open the opt-in source boundary immediately before its first write."""
        if self._conn is None:
            raise RuntimeError("unit of work is not active")
        if not self._conn.in_transaction:
            # The source observes this ambient transaction and takes a
            # savepoint, preventing it from committing the UoW boundary.
            self._conn.execute("BEGIN IMMEDIATE")

    @property
    def connection(self):
        """Expose the caller-owned connection only to application ports."""
        if self._conn is None:
            raise RuntimeError("unit of work is not active")
        return self._conn

    @property
    def authoritative_fact_source(self):
        """Return the composition-owned fact writer for application services."""
        return self._authoritative_fact_source

    @property
    def verifier_observations(self):
        """Return the verifier observation repository on this UoW connection."""
        from .persistence.sqlite.verifier_observations import (
            SQLiteVerifierObservationRepository,
        )
        return SQLiteVerifierObservationRepository(self.connection)

    @property
    def strategy_experiences(self):
        """Return the scoped strategy index in this canonical memory transaction."""
        from .persistence.sqlite.strategy_memory import (
            SQLiteStrategyExperienceRepository,
        )

        return SQLiteStrategyExperienceRepository(self.connection)

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
