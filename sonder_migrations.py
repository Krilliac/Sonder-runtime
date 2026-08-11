"""Explicit, checksummed schema migrations for Sonder's SQLite stores.

SPEC-2 section 7.  Each store owns an ordered directory of migration
modules under ``migrations/<store>/``; every applied migration is recorded
in a ``schema_migrations`` ledger inside the database it changed.  The
ledger — not code inspection — is the authority on what a database
contains, which is what makes upgrade, rollback-refusal, and
future-schema rejection decidable.

Rules enforced here:

- A process-shared migration lock in ``SONDER_HOME/locks`` serializes
  migration across processes.
- ``PRAGMA foreign_keys=ON`` and a bounded ``busy_timeout`` on every
  connection.
- One transaction per migration.
- A database whose ledger names a migration this build does not know is
  newer than the application: startup must refuse it rather than guess.
- A recorded checksum that no longer matches the migration source means
  history was edited; refuse rather than reapply.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import time
import importlib.util
from dataclasses import dataclass
from pathlib import Path

import sonder_paths
import sonder_version

MIGRATIONS_ROOT = Path(__file__).resolve().parent / "migrations"

LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    migration_id TEXT PRIMARY KEY,
    applied_at_utc TEXT NOT NULL,
    application_version TEXT NOT NULL,
    checksum_sha256 TEXT NOT NULL,
    duration_ms INTEGER NOT NULL
)
"""


class MigrationError(RuntimeError):
    pass


class FutureSchemaError(MigrationError):
    """The database was written by a newer application build."""


@dataclass(frozen=True)
class Migration:
    store: str
    migration_id: str
    path: Path
    checksum: str

    def _load(self):
        spec = importlib.util.spec_from_file_location(
            f"sonder_migration_{self.store}_{self.migration_id}", self.path
        )
        if spec is None or spec.loader is None:
            raise MigrationError(f"cannot load migration {self.path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for required in ("apply",):
            if not callable(getattr(module, required, None)):
                raise MigrationError(
                    f"migration {self.path} lacks required function {required}()"
                )
        return module

    def run(self, conn: sqlite3.Connection, record_applied=None) -> int:
        module = self._load()
        started = time.monotonic()
        preflight = getattr(module, "preflight", None)
        if callable(preflight):
            preflight(conn)
        # Adoption baselines delegate to idempotent legacy bootstraps that own
        # their transactions. Normal migrations keep their schema change and
        # ledger row in one transaction; separating them can strand a
        # non-idempotent baseline after a crash or locked ledger insert.
        if getattr(module, "manages_own_transaction", False):
            module.apply(conn)
            verify = getattr(module, "verify", None)
            if callable(verify):
                verify(conn)
            duration_ms = int((time.monotonic() - started) * 1000)
            if record_applied is not None:
                record_applied(duration_ms)
            return duration_ms
        else:
            conn.execute("BEGIN")
            try:
                module.apply(conn)
                verify = getattr(module, "verify", None)
                if callable(verify):
                    verify(conn)
                duration_ms = int((time.monotonic() - started) * 1000)
                if record_applied is not None:
                    record_applied(duration_ms)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return duration_ms


@dataclass(frozen=True)
class StoreStatus:
    store: str
    db_path: str
    applied: tuple[str, ...]
    pending: tuple[str, ...]
    unknown: tuple[str, ...]  # in ledger but not in this build => future schema
    checksum_mismatches: tuple[str, ...]

    @property
    def healthy(self) -> bool:
        return not self.unknown and not self.checksum_mismatches

    @property
    def current(self) -> bool:
        return self.healthy and not self.pending


def _operations_db_path() -> str:
    return sonder_paths.state_path("operations.db", "SONDER_OPERATIONS_DB")


# Store registry: name -> callable returning the database path.  The
# memory/autopilot/fleet baselines are established by SPEC-2 WP5; the
# framework treats a store with no migration directory as "no ledger yet".
def store_db_paths() -> dict[str, str]:
    import autopilot_store
    import fleet_store
    import queued_actions

    return {
        "memory": sonder_paths.memory_db_path(),
        "autopilot": autopilot_store.database_path(),
        "fleet": fleet_store.database_path(),
        "operations": _operations_db_path(),
        "queued_actions": queued_actions.database_path(),
        "updates": sonder_paths.state_path("updates.db", "SONDER_UPDATES_DB"),
    }


def discover_migrations(store: str) -> tuple[Migration, ...]:
    directory = MIGRATIONS_ROOT / store
    if not directory.is_dir():
        return ()
    found = []
    for path in sorted(directory.glob("[0-9][0-9][0-9][0-9]_*.py")):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        found.append(
            Migration(
                store=store,
                migration_id=path.stem,
                path=path,
                checksum=digest,
            )
        )
    return tuple(found)


def open_connection(db_path: str, *, busy_timeout_ms: int = 5000) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
    return conn


def _ledger_rows(conn: sqlite3.Connection) -> dict[str, str]:
    conn.execute(LEDGER_DDL)
    return {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT migration_id, checksum_sha256 FROM schema_migrations"
        )
    }


def status(store: str, db_path: str | None = None) -> StoreStatus:
    path = db_path or store_db_paths()[store]
    migrations = discover_migrations(store)
    known = {m.migration_id: m for m in migrations}
    if not Path(path).exists() and not migrations:
        return StoreStatus(store, path, (), (), (), ())
    conn = open_connection(path)
    try:
        recorded = _ledger_rows(conn)
    finally:
        conn.close()
    applied = tuple(mid for mid in known if mid in recorded)
    pending = tuple(mid for mid in known if mid not in recorded)
    unknown = tuple(sorted(mid for mid in recorded if mid not in known))
    mismatched = tuple(
        mid
        for mid in applied
        if recorded[mid] != known[mid].checksum
    )
    return StoreStatus(store, path, applied, pending, unknown, mismatched)


def status_read_only(store: str, db_path: str) -> StoreStatus:
    """Inspect one migration ledger without creating a database or table."""
    path = Path(db_path).expanduser()
    migrations = discover_migrations(store)
    known = {migration.migration_id: migration for migration in migrations}
    if not path.is_file():
        return StoreStatus(store, str(path), (), tuple(known), (), ())

    uri = path.resolve(strict=True).as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        has_ledger = conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        recorded = (
            {
                row[0]: row[1]
                for row in conn.execute(
                    "SELECT migration_id, checksum_sha256 "
                    "FROM schema_migrations"
                )
            }
            if has_ledger
            else {}
        )
    finally:
        conn.close()
    applied = tuple(mid for mid in known if mid in recorded)
    pending = tuple(mid for mid in known if mid not in recorded)
    unknown = tuple(sorted(mid for mid in recorded if mid not in known))
    mismatched = tuple(
        mid for mid in applied if recorded[mid] != known[mid].checksum
    )
    return StoreStatus(
        store, str(path), applied, pending, unknown, mismatched
    )


def status_all_read_only(home: str) -> dict[str, StoreStatus]:
    """Inspect all configured stores below *home* without mutating state."""
    root = Path(home).expanduser()
    names = {
        "memory": "memory.db",
        "autopilot": "autopilot.db",
        "fleet": "fleet.db",
        "operations": "operations.db",
        "queued_actions": "queued_actions.db",
        "updates": "updates.db",
    }
    return {
        store: status_read_only(store, str(root / filename))
        for store, filename in names.items()
    }


class _FileLock:
    """Cross-process advisory lock (fcntl on POSIX, msvcrt on Windows)."""

    def __init__(self, path: Path, timeout: float = 30.0) -> None:
        self._path = path
        self._timeout = timeout
        self._handle = None

    def __enter__(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self._timeout
        self._handle = open(self._path, "a+")
        while True:
            try:
                if os.name == "nt":  # pragma: no cover - windows only
                    import msvcrt

                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    self._handle.close()
                    self._handle = None
                    raise MigrationError(
                        f"could not acquire migration lock {self._path} "
                        f"within {self._timeout}s"
                    ) from None
                time.sleep(0.1)

    def __exit__(self, exc_type, exc, tb):
        if self._handle is not None:
            try:
                if os.name == "nt":  # pragma: no cover - windows only
                    import msvcrt

                    self._handle.seek(0)
                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._handle, fcntl.LOCK_UN)
            finally:
                self._handle.close()
                self._handle = None


def migration_lock(timeout: float = 30.0) -> _FileLock:
    lock_dir = sonder_paths.ensure_home() / "locks"
    return _FileLock(lock_dir / "migrations.lock", timeout=timeout)


def migrate_store(
    store: str,
    db_path: str | None = None,
    *,
    busy_timeout_ms: int = 5000,
) -> StoreStatus:
    """Apply pending migrations for one store, honoring every SPEC-2 rule."""
    path = db_path or store_db_paths()[store]
    migrations = discover_migrations(store)
    if not migrations:
        # Returning `status(...)` unconditionally here skipped the two gates
        # below. A build whose `migrations/` directory was omitted from the
        # release bundle discovers nothing, so it compared a database full of
        # applied history against an empty set of known migrations and reported
        # a clean, empty result -- the caller saw exit 0 with no pending work
        # and activated. The evidence that the build had LOST its schema
        # definitions is exactly the signal `status()` already computes: every
        # recorded row is now unknown. Do not discard it.
        current = status(store, path)
        if current.unknown:
            raise FutureSchemaError(
                f"store {store!r} at {path} records {len(current.unknown)} applied "
                f"migration(s) this build does not define and found no migrations of "
                f"its own: {', '.join(current.unknown)}. Either this database is "
                f"newer than the code, or this build shipped without its "
                f"migrations/ directory; refusing to run"
            )
        return current

    with migration_lock():
        current = status(store, path)
        if current.unknown:
            raise FutureSchemaError(
                f"store {store!r} at {path} records migrations unknown to "
                f"this build: {', '.join(current.unknown)}; refusing to run"
            )
        if current.checksum_mismatches:
            raise MigrationError(
                f"store {store!r} migration history was modified: "
                f"{', '.join(current.checksum_mismatches)}"
            )
        version = sonder_version.build_info().version
        conn = open_connection(path, busy_timeout_ms=busy_timeout_ms)
        try:
            recorded = _ledger_rows(conn)
            for migration in migrations:
                if migration.migration_id in recorded:
                    continue

                def record_applied(duration_ms, migration=migration):
                    conn.execute(
                        "INSERT INTO schema_migrations (migration_id, applied_at_utc,"
                        " application_version, checksum_sha256, duration_ms)"
                        " VALUES (?, ?, ?, ?, ?)",
                        (
                            migration.migration_id,
                            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            version,
                            migration.checksum,
                            duration_ms,
                        ),
                    )

                migration.run(conn, record_applied=record_applied)
        finally:
            conn.close()
    return status(store, path)


def migrate_all(*, busy_timeout_ms: int = 5000) -> dict[str, StoreStatus]:
    return {
        store: migrate_store(store, path, busy_timeout_ms=busy_timeout_ms)
        for store, path in store_db_paths().items()
    }


def status_all() -> dict[str, StoreStatus]:
    return {store: status(store, path) for store, path in store_db_paths().items()}
