"""From-inception disposable host; not an installed service-manager adapter.

Only this host's new private namespace, Application and launch gate are covered.
An existing directory, unrelated Application or unknown connection is never
adopted as evidence. Losing the host object/process loses activation authority.
"""

from contextlib import contextmanager
from pathlib import Path
from threading import RLock
import os
import json

from .app import build_application
from .child_storage import HostChildRepositoryFactory
from ..adapters.filesystem.atomic_json import file_lock, write_json_atomic
from ..adapters.persistence.durable_continuation import (
    SQLiteDurableContinuationRepository,
)
from ..adapters.persistence.child_migration import SQLiteChildMigrationStore
from ..adapters.persistence.postgres_continuation import (
    PostgreSQLDurableContinuationRepository,
)
from ..adapters.persistence.postgres_binding import PostgresPrivateBinding
from ..application.compute_fabric.artifact_spool import PrivateDirectoryAnchor
from ..application.subagents.child_migration import (
    MigrationRefused,
    MigrationUnsupported,
    STREAMS,
    stream_descriptor,
    verify_snapshot,
    digest,
)
from ..application.subagents.child_migration_activation import (
    issue_host_guard,
    _register_host_issuer,
    _unregister_host_issuer,
)


from .child_migration_activation import ChildMigrationActivation


class DisposableChildMigrationHost(ChildMigrationActivation):
    def __init__(self, path, *, writable_roots):
        self.path = Path(path).absolute()
        if self.path.exists():
            raise MigrationUnsupported(
                "cannot adopt an existing or installed migration namespace"
            )
        self._roots = writable_roots
        for root in self._roots():
            root = Path(root).resolve()
            if (
                self.path == root
                or self.path.is_relative_to(root)
                or root.is_relative_to(self.path)
            ):
                raise MigrationRefused("migration namespace overlaps writable roots")
        self._anchor = PrivateDirectoryAnchor.open_base(self.path)
        self._lock = RLock()
        self._launch = file_lock(self.path / "launch", timeout=0)
        self._launch.__enter__()
        self._live = True
        self._application = self._repository = None
        self._tracked = 0
        self._cutover_manifest = self._cutover_bundle = None
        self._cutover_selection = None
        self._cutover_stores = ()
        self._selection = SQLiteChildMigrationStore(self.path / "children.sqlite")
        self._validate()
        _register_host_issuer(self, self._require_quiescent)


    @property
    def selected_store(self):
        return self._selection

    def _validate(self):
        if not self._live:
            raise MigrationRefused("migration host launch exclusion is no longer held")
        self._anchor.validate()
        for root in self._roots():
            root = Path(root).resolve()
            if (
                self.path == root
                or self.path.is_relative_to(root)
                or root.is_relative_to(self.path)
            ):
                raise MigrationRefused("migration namespace overlaps writable roots")


    def start(self, config):
        with self._lock:
            self._validate()
            if self._cutover_manifest is not None:
                raise MigrationRefused(
                    "activation is incomplete; reconcile the same migration ID"
                )
            if self._application is not None:
                raise MigrationRefused("owned application already started")
            store = self._selection
            backend = (
                "sqlite"
                if isinstance(store, SQLiteChildMigrationStore)
                else "postgresql"
            )
            if config.child_storage.backend != backend:
                raise MigrationRefused(
                    "host selection conflicts with configured backend"
                )
            if backend == "postgresql":
                if config.child_storage != store.config:
                    raise MigrationRefused("host PostgreSQL binding or policy changed")
                if not store.close():
                    raise MigrationRefused("migration store cleanup is incomplete")

            def create():
                self._validate()
                if self._repository is not None:
                    raise MigrationRefused("owned repository already exists")
                if backend == "sqlite":
                    repository = SQLiteDurableContinuationRepository(store.path)
                else:
                    binding = PostgresPrivateBinding(
                        config.child_storage.binding_file, writable_roots=self._roots
                    )
                    try:
                        store.validate_policy(binding)
                    except BaseException:
                        binding.close()
                        raise
                    repository = PostgreSQLDurableContinuationRepository(
                        config.child_storage, binding
                    )
                self._repository = repository
                return repository

            try:
                application = build_application(
                    config=config,
                    child_repository_factory=HostChildRepositoryFactory(
                        backend, create
                    ),
                )
                application.delegation_service()
                self._application = application
                return application
            except BaseException:
                if self._repository is not None:
                    self._repository.close(runners_stopped=True, timeout=5)
                raise

    @contextmanager
    def tracked_connection(self):
        """A known host-owned SQLite handle participates in cleanup evidence."""
        with self._lock:
            self._validate()
            if self._repository is None or not isinstance(
                self._repository, SQLiteDurableContinuationRepository
            ):
                raise MigrationRefused("no owned SQLite application")
            self._tracked += 1
        try:
            with self._repository._connect() as connection:
                yield connection
        finally:
            with self._lock:
                self._tracked -= 1

    def quiesce(self, timeout=5):
        with self._lock:
            self._validate()
            if self._tracked:
                raise MigrationRefused("owned database connections remain live")
            if self._application is not None:
                self._application.close_delegation(timeout=timeout)
                self._application.close_compute()
                self._application = None
            if self._repository is not None:
                if not self._repository.close(runners_stopped=True, timeout=timeout):
                    raise MigrationRefused("owned database cleanup remains incomplete")
                self._repository = None


    def close(self):
        with self._lock:
            if not self._live:
                return
            self.quiesce()
            close = getattr(self._selection, "close", None)
            if close is not None and not close():
                raise MigrationRefused("selected migration store has pending cleanup")
            self._live = False
            self._cutover_manifest = self._cutover_bundle = None
            _unregister_host_issuer(self)
            self._launch.__exit__(None, None, None)
            self._anchor.close()
