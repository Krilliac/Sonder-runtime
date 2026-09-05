"""SQLite transfer transactions for the canonical durable child aggregate."""

from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sqlite3

from ...application.subagents.child_migration import (
    MigrationRefused,
    binary,
    unbinary,
    digest,
    encode,
    validate_record,
)
from ...application.subagents.continuation_codec import session_from_data
from ...application.ports.continuation_mutations import canonical
from .durable_continuation import (
    SQLiteDurableContinuationRepository,
    _DDL,
    _budget_json,
    _usage_json,
    _result_json,
)

_MIGRATION_DDL = """
CREATE TABLE continuation_migration(id INTEGER PRIMARY KEY CHECK(id=1),migration_id TEXT NOT NULL,manifest_digest TEXT NOT NULL,phase TEXT NOT NULL,manifest BLOB NOT NULL);
CREATE TABLE continuation_migration_page(stream TEXT NOT NULL,page INTEGER NOT NULL,digest TEXT NOT NULL,count INTEGER NOT NULL,last_position INTEGER NOT NULL,PRIMARY KEY(stream,page));
CREATE TABLE continuation_migration_watermark(id INTEGER PRIMARY KEY CHECK(id=1),children_high_water INTEGER NOT NULL);
"""


def _root(child):
    request = child.request
    return (
        request.parent_id == request.child_id
        and request.prompt == "local provider root"
        and request.metadata == (("provider_root", "true"),)
        and not child.lineage.ancestors
    )


class SQLiteChildSnapshot:
    def __init__(self, connection):
        self.connection = connection

    def records(self, stream):
        if stream == "children":
            for row in self.connection.execute(
                "SELECT rowid,* FROM durable_child_session ORDER BY rowid"
            ):
                child = SQLiteDurableContinuationRepository._row(row[1:])
                yield {
                    "position": row[0],
                    "key": child.request.child_id,
                    "snapshot": binary(canonical(asdict(child))),
                }
        elif stream == "intents":
            for row in self.connection.execute(
                "SELECT position,operation_id,child_id,kind,digest,payload FROM continuation_intent ORDER BY position"
            ):
                yield dict(
                    zip(
                        ("position", "key", "child_id", "kind", "digest", "payload"),
                        (*row[:5], binary(row[5])),
                    )
                )
        elif stream == "receipts":
            for row in self.connection.execute(
                "SELECT i.position,r.operation_id,r.disposition,r.result,r.revision FROM continuation_receipt r JOIN continuation_intent i USING(operation_id) ORDER BY i.position"
            ):
                yield {
                    "position": row[0],
                    "key": row[1],
                    "disposition": row[2],
                    "result": binary(row[3]),
                    "revision": row[4],
                }
        else:
            raise MigrationRefused("unknown migration stream")

    def metadata(self):
        missing = self.connection.execute(
            "SELECT count(*) FROM durable_child_session c WHERE c.parent_id<>c.child_id AND NOT EXISTS(SELECT 1 FROM durable_child_session p WHERE p.child_id=c.parent_id)"
        ).fetchone()[0]
        if missing:
            raise MigrationRefused("source contains a missing durable parent")
        unresolved = self.connection.execute(
            "SELECT count(*) FROM continuation_intent i LEFT JOIN continuation_receipt r USING(operation_id) WHERE r.operation_id IS NULL"
        ).fetchone()[0]
        active = 0
        for row in self.connection.execute(
            "SELECT * FROM durable_child_session WHERE status NOT IN ('succeeded','failed','timed_out','cancelled')"
        ):
            if not _root(SQLiteDurableContinuationRepository._row(row)):
                active += 1
        sequence = self.connection.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='continuation_intent'"
        ).fetchone()
        children_high = self.connection.execute(
            "SELECT coalesce(max(rowid),0) FROM durable_child_session"
        ).fetchone()[0]
        if self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='continuation_migration_watermark'"
        ).fetchone():
            row = self.connection.execute(
                "SELECT children_high_water FROM continuation_migration_watermark WHERE id=1"
            ).fetchone()
            if row is not None:
                children_high = max(children_high, row[0])
        return {
            "backend": "sqlite",
            "schema": 1,
            "unresolved": unresolved,
            "active": active,
            "children_high_water": children_high,
            "intents_high_water": sequence[0] if sequence else 0,
            "owner": None,
            "barrier": None,
        }


class SQLiteChildMigrationStore:
    def __init__(self, path):
        self.path = Path(path).absolute()
        self.identity = hashlib.sha256(str(self.path).encode()).hexdigest()

    def read_snapshot(self, function, *, bundle=None):
        with self.snapshot(bundle=bundle) as snapshot:
            return function(snapshot)

    def physical_identity(self):
        rows = []
        for suffix in ("", "-wal", "-shm", "-journal"):
            item = Path(str(self.path) + suffix)
            if not item.exists():
                rows.append(None)
                continue
            if item.is_symlink() or not item.is_file():
                raise MigrationRefused("source database is not a regular file")
            stat = item.stat()
            rows.append([stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns])
        return rows

    @contextmanager
    def snapshot(self, *, bundle=None):
        if not self.path.is_file() or self.path.is_symlink():
            raise MigrationRefused("migration source is not a regular database")
        source = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=1)
        backup = None
        try:
            if bundle is not None:
                # The supported backup API includes WAL content. The destination
                # is inside the private bundle, never a raw live-file copy.
                backup_path = bundle.path / "source-backup.sqlite"
                if bundle.has_sealed_backup():
                    source.close()
                    source = sqlite3.connect(
                        backup_path.as_uri() + "?mode=ro", uri=True, timeout=1
                    )
                elif backup_path.exists():
                    raise MigrationRefused(
                        "source backup completion is unknown; retain this bundle for reconciliation"
                    )
                else:
                    self._backup(source, backup_path)
                    source.close()
                    source = sqlite3.connect(
                        backup_path.as_uri() + "?mode=ro", uri=True, timeout=1
                    )
                    bundle.seal_backup(self.physical_identity())
            source.execute("BEGIN")
            if (
                source.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
                or source.execute("PRAGMA foreign_key_check").fetchone() is not None
            ):
                raise MigrationRefused("migration source integrity failed")
            yield SQLiteChildSnapshot(source)
            source.rollback()
        finally:
            source.close()
            if backup is not None:
                backup.close()

    @staticmethod
    def _backup(source, path):
        import time

        backup = sqlite3.connect(path)
        try:
            deadline = time.monotonic() + 30
            page_size = source.execute("PRAGMA page_size").fetchone()[0]

            def progress(status, remaining, total):
                if (
                    time.monotonic() >= deadline
                    or total * page_size > 512 * 1024 * 1024
                ):
                    raise MigrationRefused(
                        "source backup exceeded time or capacity bound"
                    )

            source.backup(backup, pages=128, progress=progress, sleep=0.05)
            backup.commit()
        finally:
            backup.close()

    @contextmanager
    def _write(self):
        connection = sqlite3.connect(self.path, timeout=1)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _current(connection, manifest):
        row = connection.execute(
            "SELECT migration_id,manifest_digest,phase FROM continuation_migration WHERE id=1"
        ).fetchone()
        if row is None or row[:2] != (manifest["migration_id"], digest(manifest)):
            raise MigrationRefused("migration target manifest conflict")
        return row[2]

    def prepare(self, manifest):
        existed = self.path.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._write() as connection:
            if existed:
                if (
                    connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE name='continuation_migration'"
                    ).fetchone()
                    is None
                ):
                    raise MigrationRefused("migration target is not isolated")
                self._current(connection, manifest)
                return
            for statement in (_DDL + _MIGRATION_DDL).split(";"):
                if statement.strip():
                    connection.execute(statement)
            connection.execute(
                "INSERT INTO continuation_migration VALUES(1,?,?,?,?)",
                (
                    manifest["migration_id"],
                    digest(manifest),
                    "COPYING",
                    encode(manifest),
                ),
            )

    def status(self, manifest):
        if not self.path.is_file():
            return {"phase": "UNSTAGED", "migration_id": manifest["migration_id"]}
        connection = sqlite3.connect(
            self.path.as_uri() + "?mode=ro", uri=True, timeout=1
        )
        try:
            phase = self._current(connection, manifest)
        finally:
            connection.close()
        return {"phase": phase, "migration_id": manifest["migration_id"]}

    def copy_page(self, manifest, stream, index, records):
        page_digest = digest(records)
        with self._write() as connection:
            phase = self._current(connection, manifest)
            prior = connection.execute(
                "SELECT digest FROM continuation_migration_page WHERE stream=? AND page=?",
                (stream, index),
            ).fetchone()
            if prior:
                if prior[0] != page_digest:
                    raise MigrationRefused("migration page identity conflict")
                return
            expected = connection.execute(
                "SELECT count(*) FROM continuation_migration_page WHERE stream=?",
                (stream,),
            ).fetchone()[0]
            if phase != "COPYING" or index != expected:
                raise MigrationRefused("migration page is out of order")
            for record in records:
                validate_record(stream, record)
                self._insert(connection, stream, record)
            connection.execute(
                "INSERT INTO continuation_migration_page VALUES(?,?,?,?,?)",
                (stream, index, page_digest, len(records), records[-1]["position"]),
            )

    @staticmethod
    def _insert(connection, stream, row):
        if stream == "children":
            child = session_from_data(json.loads(unbinary(row["snapshot"])))
            request, checkpoint = child.request, child.checkpoint
            values = (
                row["position"],
                request.child_id,
                request.parent_id,
                json.dumps(child.lineage.ancestors),
                request.prompt,
                _budget_json(request.budget),
                json.dumps(request.metadata),
                child.status.value,
                checkpoint.sequence if checkpoint else None,
                json.dumps(checkpoint.state) if checkpoint else None,
                checkpoint.cursor if checkpoint else None,
                child.revision,
                _usage_json(child.usage),
                _result_json(child.result),
                int(child.recovery_required),
                int(child.cancellation_requested),
                child.cancellation_reason,
            )
            connection.execute(
                "INSERT INTO durable_child_session(rowid,child_id,parent_id,ancestors_json,prompt,budget_json,metadata_json,status,checkpoint_sequence,checkpoint_state_json,checkpoint_cursor,revision,usage_json,result_json,recovery_required,cancellation_requested,cancellation_reason) VALUES("
                + ",".join("?" for _ in values)
                + ")",
                values,
            )
        elif stream == "intents":
            connection.execute(
                "INSERT INTO continuation_intent VALUES(?,?,?,?,?,?)",
                (
                    row["position"],
                    row["key"],
                    row["child_id"],
                    row["kind"],
                    row["digest"],
                    unbinary(row["payload"]),
                ),
            )
        else:
            connection.execute(
                "INSERT INTO continuation_receipt VALUES(?,?,?,?)",
                (
                    row["key"],
                    row["disposition"],
                    unbinary(row["result"]),
                    row["revision"],
                ),
            )

    def copied(self, manifest):
        with self._write() as connection:
            if self._current(connection, manifest) != "COPYING":
                return
            connection.execute(
                "DELETE FROM sqlite_sequence WHERE name='continuation_intent'"
            )
            connection.execute(
                "INSERT INTO sqlite_sequence(name,seq) VALUES('continuation_intent',?)",
                (manifest["source"]["intents_high_water"],),
            )
            connection.execute(
                "INSERT INTO continuation_migration_watermark VALUES(1,?)",
                (manifest["source"]["children_high_water"],),
            )
            connection.execute(
                "UPDATE continuation_migration SET phase='COPIED' WHERE id=1"
            )

    def verified(self, manifest):
        with self._write() as connection:
            phase = self._current(connection, manifest)
            if phase in ("VERIFIED", "ACTIVE"):
                return
            if phase not in ("COPIED", "VERIFIED"):
                raise MigrationRefused("migration target is not ready for verification")
            connection.execute(
                "UPDATE continuation_migration SET phase='VERIFIED' WHERE id=1"
            )

    def activate(self, manifest, guard):
        from ...application.subagents.child_migration_activation import (
            require_host_guard,
        )

        require_host_guard(guard, manifest)
        with self._write() as connection:
            if self._current(connection, manifest) not in ("VERIFIED", "ACTIVE"):
                raise MigrationRefused("migration target is not verified")
            require_host_guard(guard, manifest)
            connection.execute(
                "UPDATE continuation_migration SET phase='ACTIVE' WHERE id=1"
            )
        require_host_guard(guard, manifest)
