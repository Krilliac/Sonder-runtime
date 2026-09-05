"""From-inception disposable host; not an installed service-manager adapter.

Only this host's new private namespace, Application and launch gate are covered.
An existing directory, unrelated Application or unknown connection is never
adopted as evidence. Losing the host object/process loses activation authority.
"""

from contextlib import contextmanager
from pathlib import Path
from threading import RLock
import os

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


class DisposableChildMigrationHost:
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
        self._selection = SQLiteChildMigrationStore(self.path / "children.sqlite")
        self._validate()
        _register_host_issuer(self, self._require_quiescent)

    def _require_quiescent(self, manifest):
        self._validate()
        if self._cutover_manifest != digest(manifest) or self._cutover_bundle is None:
            raise MigrationRefused(
                "migration issuer has not verified this exact cutover"
            )
        self._cutover_bundle.validate()
        if (
            self._application is not None
            or self._repository is not None
            or self._tracked
        ):
            raise MigrationRefused("owned cleanup proof is no longer current")

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
            self._cutover_manifest = self._cutover_bundle = None
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

    def activate(self, bundle, source, target, *, timeout=5):
        with self._lock:
            self.quiesce(timeout)
            manifest = bundle.manifest()
            if (
                bundle.has_phase("COMPLETE", manifest)
                and self._selection.identity == target.identity
            ):
                return {"phase": "COMPLETE", "migration_id": manifest["migration_id"]}
            if (
                source.identity != self._selection.identity
                or manifest["source_identity"] != source.identity
                or manifest["target_identity"] != target.identity
            ):
                raise MigrationRefused(
                    "migration does not match the owned current selection"
                )
            if (
                isinstance(target, SQLiteChildMigrationStore)
                and target.path.parent != self.path
            ):
                raise MigrationUnsupported(
                    "target SQLite path is outside the owned namespace"
                )

            # A fresh source comparison rejects a stale original backup after
            # any new application writes. No saved hash is cleanup authority.
            def compare(snapshot):
                metadata = snapshot.metadata()
                if metadata["active"] or metadata["unresolved"]:
                    raise MigrationRefused("source execution is not quiescent")
                for stream in STREAMS:
                    if (
                        stream_descriptor(stream, snapshot.records(stream))
                        != manifest["streams"][stream]
                    ):
                        raise MigrationRefused(
                            "source changed after export; a fresh snapshot is required"
                        )
                for key in ("children_high_water", "intents_high_water"):
                    if metadata[key] != manifest["source"][key]:
                        raise MigrationRefused("source ordering changed after export")

            retired = self.path / ("retired-" + manifest["migration_id"] + ".sqlite")
            already_retired = bundle.has_phase("SOURCE_RETIRED", manifest)
            if isinstance(source, SQLiteChildMigrationStore) and (
                already_retired
                or (
                    source.path.is_dir()
                    and retired.is_file()
                    and bundle.has_phase("SOURCE_RETIRE_INTENT", manifest)
                )
            ):
                SQLiteChildMigrationStore(retired).read_snapshot(compare)
                already_retired = True
            else:
                source.read_snapshot(compare)
            verify_snapshot(bundle, target)

            def live():
                self._validate()
                bundle.validate()
                if (
                    self._application is not None
                    or self._repository is not None
                    or self._tracked
                ):
                    raise MigrationRefused("owned cleanup proof is no longer current")

            self._cutover_manifest, self._cutover_bundle = digest(manifest), bundle
            guard = issue_host_guard(self, manifest)
            bundle.record_phase("SOURCE_RETIRE_INTENT", manifest)
            if already_retired:
                pass
            elif isinstance(source, SQLiteChildMigrationStore):
                if source.path.parent != self.path:
                    raise MigrationUnsupported(
                        "SQLite source was not created in the owned namespace"
                    )
                # The gate and owned-handle cleanup are the authority. The
                # directory only blocks future opens by older SQLite binaries.
                before = source.physical_identity()
                if before != manifest["source_files"]:
                    raise MigrationRefused(
                        "source file or sidecar changed after export"
                    )
                live()
                if source.physical_identity() != before:
                    raise MigrationRefused("source file identity changed")
                if retired.exists():
                    raise MigrationRefused(
                        "source retirement needs same-ID reconciliation"
                    )
                os.rename(source.path, retired)
                source.path.mkdir()
            else:
                source.retire(manifest, guard)
            bundle.record_phase("SOURCE_RETIRED", manifest)
            target.activate(manifest, guard)
            bundle.record_phase("TARGET_READY", manifest)
            live()
            write_json_atomic(
                self.path / "selection.json",
                {
                    "migration_id": manifest["migration_id"],
                    "manifest_digest": digest(manifest),
                    "target_identity": target.identity,
                },
            )
            self._selection = target
            bundle.record_phase("CONFIG_SWITCHED", manifest)
            bundle.record_phase("COMPLETE", manifest)
            self._cutover_manifest = self._cutover_bundle = None
            return {"phase": "COMPLETE", "migration_id": manifest["migration_id"]}

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
