"""PostgreSQL offline transfer while holding the aggregate admission lock."""

from contextlib import contextmanager
import hashlib
import json
import uuid

from ...application.subagents.child_migration import (
    MigrationRefused,
    binary,
    unbinary,
    digest,
    encode,
    validate_record,
)
from ...application.subagents.continuation_codec import session_from_data
from ...application.ports.continuation_mutations import ContinuationStorageFailure
from .child_migration import _root
from .postgres_continuation import PostgreSQLDurableContinuationRepository, _OWNER_KEY
from .postgres_continuation_transport import PostgresContinuationTransport


class PostgresChildSnapshot:
    def __init__(self, connection):
        self.connection = connection

    def records(self, stream):
        if stream == "children":
            query = "SELECT position,child_id,snapshot FROM sonder_child.child ORDER BY position"
        elif stream == "intents":
            query = "SELECT position,operation_id,child_id,kind,digest,payload FROM sonder_child.intent ORDER BY position"
        else:
            query = "SELECT i.position,r.operation_id,r.disposition,r.result,r.revision FROM sonder_child.receipt r JOIN sonder_child.intent i USING(operation_id) ORDER BY i.position"
        # A named cursor keeps libpq from buffering the entire aggregate.
        with self.connection.cursor(name="child_migration_" + stream) as cursor:
            cursor.itersize = 1
            cursor.execute(query)
            for row in cursor:
                if stream == "children":
                    yield {
                        "position": row[0],
                        "key": row[1],
                        "snapshot": binary(bytes(row[2])),
                    }
                elif stream == "intents":
                    yield dict(
                        zip(
                            (
                                "position",
                                "key",
                                "child_id",
                                "kind",
                                "digest",
                                "payload",
                            ),
                            (*row[:5], binary(bytes(row[5]))),
                        )
                    )
                else:
                    yield {
                        "position": row[0],
                        "key": row[1],
                        "disposition": row[2],
                        "result": binary(bytes(row[3])),
                        "revision": row[4],
                    }

    def metadata(self):
        missing = self.connection.execute(
            "SELECT count(*) FROM sonder_child.child c WHERE c.child_id<>(convert_from(c.snapshot,'UTF8')::jsonb->'request'->>'parent_id') AND NOT EXISTS(SELECT 1 FROM sonder_child.child p WHERE p.child_id=(convert_from(c.snapshot,'UTF8')::jsonb->'request'->>'parent_id'))"
        ).fetchone()[0]
        if missing:
            raise MigrationRefused("source contains a missing durable parent")
        unresolved = self.connection.execute(
            "SELECT count(*) FROM sonder_child.intent i LEFT JOIN sonder_child.receipt r USING(operation_id) WHERE r.operation_id IS NULL"
        ).fetchone()[0]
        active = 0
        for row in self.records("children"):
            child = session_from_data(json.loads(unbinary(row["snapshot"])))
            if child.status.value not in (
                "succeeded",
                "failed",
                "timed_out",
                "cancelled",
            ) and not _root(child):
                active += 1
        child_sequence = self.connection.execute(
            "SELECT last_value,is_called FROM sonder_child.child_position_seq"
        ).fetchone()
        intent_sequence = self.connection.execute(
            "SELECT last_value,is_called FROM sonder_child.intent_position_seq"
        ).fetchone()
        owner = self.connection.execute(
            "SELECT owner_id,incarnation,clean FROM sonder_child.owner WHERE id=1"
        ).fetchone()
        return {
            "backend": "postgresql",
            "schema": 1,
            "unresolved": unresolved,
            "active": active,
            "children_high_water": child_sequence[0] if child_sequence[1] else 0,
            "intents_high_water": intent_sequence[0] if intent_sequence[1] else 0,
            "owner": list(owner) if owner else None,
            "barrier": self.connection.execute(
                "SELECT barrier FROM sonder_child.meta WHERE id=1"
            ).fetchone()[0],
        }


class PostgresChildMigrationStore:
    _policy = PostgreSQLDurableContinuationRepository._policy
    _begin = PostgreSQLDurableContinuationRepository._begin
    _schema = PostgreSQLDurableContinuationRepository._schema

    def __init__(self, config, binding):
        from types import SimpleNamespace
        from ...platform.child_storage_config import child_storage_errors

        if child_storage_errors(SimpleNamespace(child_storage=config)):
            raise MigrationRefused("invalid PostgreSQL migration configuration")
        self.config, self.binding = config, binding
        # Identity binds the host-selected endpoint without exposing credentials.
        values = binding.connection_kwargs(config)
        self.identity = hashlib.sha256(
            encode({key: values[key] for key in ("host", "port", "dbname", "user")})
        ).hexdigest()
        self._transport = PostgresContinuationTransport(config, binding)
        self._owner_connection = None
        self._closed = False
        try:
            self._owner_connection = self._transport.connection_class.connect(**values)

            def lock(connection):
                if not connection.execute(
                    "SELECT pg_try_advisory_lock(%s,%s)", _OWNER_KEY
                ).fetchone()[0]:
                    raise MigrationRefused(
                        "migration aggregate has an active execution owner"
                    )
                self._owner_identity = connection.execute(
                    "SELECT pid,backend_start FROM pg_stat_activity WHERE pid=pg_backend_pid()"
                ).fetchone()

            self._transport.run(lock, connection=self._owner_connection)
            self._transport.run(self._schema)

            def check(connection):
                self._begin(connection)
                self._held(connection)
                owner = connection.execute(
                    "SELECT clean FROM sonder_child.owner WHERE id=1"
                ).fetchone()
                if owner is not None and not owner[0]:
                    raise MigrationRefused("migration owner cleanup is unproven")
                if owner is None:
                    count = connection.execute(
                        "SELECT (SELECT count(*) FROM sonder_child.child)+(SELECT count(*) FROM sonder_child.intent)+(SELECT count(*) FROM sonder_child.receipt)"
                    ).fetchone()[0]
                    migration = connection.execute(
                        "SELECT to_regclass('sonder_child.migration')"
                    ).fetchone()[0]
                    if count and not migration:
                        raise MigrationRefused(
                            "source execution owner metadata is missing"
                        )
                connection.rollback()

            self._transport.run(check)

            def namespace(connection):
                self._begin(connection)
                self._held(connection)
                exists = connection.execute(
                    "SELECT to_regclass('sonder_child.migration_namespace')"
                ).fetchone()[0]
                if not exists:
                    connection.execute(
                        "CREATE TABLE sonder_child.migration_namespace(id integer PRIMARY KEY CHECK(id=1),identity text NOT NULL)"
                    )
                    connection.execute(
                        "INSERT INTO sonder_child.migration_namespace VALUES(1,%s)",
                        (uuid.uuid4().hex,),
                    )
                row = connection.execute(
                    "SELECT identity FROM sonder_child.migration_namespace WHERE id=1"
                ).fetchone()
                if row is None or len(row[0]) != 32:
                    raise MigrationRefused("migration namespace identity is missing")
                connection.commit()
                self._begin(connection)
                self._held(connection)
                connection.rollback()
                return row[0]

            namespace_id = self._transport.run(namespace)
            self.identity = hashlib.sha256(
                encode({"endpoint": self.identity, "namespace": namespace_id})
            ).hexdigest()
        except BaseException:
            self.close()
            raise

    def _held(self, connection):
        if self._closed or self._owner_connection.closed:
            raise MigrationRefused("migration lock session was lost")
        held = connection.execute(
            "SELECT EXISTS(SELECT 1 FROM pg_locks l JOIN pg_stat_activity a ON l.pid=a.pid WHERE l.locktype='advisory' AND l.pid=%s AND a.backend_start=%s AND l.classid=%s AND l.objid=%s AND l.granted)",
            (*self._owner_identity, *_OWNER_KEY),
        ).fetchone()[0]
        if not held:
            raise MigrationRefused("migration lock identity was lost")

    def close(self, timeout=5):
        if self._closed:
            return True
        if not self._transport.close(timeout):
            return False
        if self._owner_connection is not None:
            self._owner_connection.close()
        self._closed = True
        self.binding.close()
        return True

    def read_snapshot(self, function, *, bundle=None):
        # The same bounded transport owns fetching and cancellation. A named
        # cursor bounds buffered rows; the external deadline bounds the whole
        # operation, including client-side encoding and filesystem output.
        if not self._transport.quiescent():
            raise MigrationRefused("migration SQL cleanup is unresolved")

        def read(connection):
            connection.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            self._held(connection)
            result = function(PostgresChildSnapshot(connection))
            self._held(connection)
            connection.rollback()
            return result

        return self._transport.run(read)

    def _write(self, function):
        if not self._transport.quiescent():
            raise MigrationRefused("migration SQL cleanup is unresolved")

        def write(connection):
            self._begin(connection)
            self._held(connection)
            value = function(connection)
            connection.commit()
            self._begin(connection)
            self._held(connection)
            connection.rollback()
            return value

        return self._transport.run(write)

    @staticmethod
    def _current(connection, manifest):
        row = connection.execute(
            "SELECT migration_id,manifest_digest,phase FROM sonder_child.migration WHERE id=1 FOR UPDATE"
        ).fetchone()
        if row is None or row[:2] != (manifest["migration_id"], digest(manifest)):
            raise MigrationRefused("migration target manifest conflict")
        return row[2]

    def prepare(self, manifest):
        def prepare(connection):
            exists = connection.execute(
                "SELECT to_regclass('sonder_child.migration')"
            ).fetchone()[0]
            if exists:
                self._current(connection, manifest)
                return
            count = connection.execute(
                "SELECT (SELECT count(*) FROM sonder_child.child)+(SELECT count(*) FROM sonder_child.intent)+(SELECT count(*) FROM sonder_child.receipt)+(SELECT count(*) FROM sonder_child.owner)"
            ).fetchone()[0]
            if count:
                raise MigrationRefused("migration target is not isolated")
            connection.execute(
                "CREATE TABLE sonder_child.migration(id integer PRIMARY KEY CHECK(id=1),migration_id text NOT NULL,manifest_digest text NOT NULL,phase text NOT NULL,manifest bytea NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE sonder_child.migration_page(stream text NOT NULL,page integer NOT NULL,digest text NOT NULL,count integer NOT NULL,last_position bigint NOT NULL,PRIMARY KEY(stream,page))"
            )
            connection.execute(
                "INSERT INTO sonder_child.migration VALUES(1,%s,%s,%s,%s)",
                (
                    manifest["migration_id"],
                    digest(manifest),
                    "COPYING",
                    encode(manifest),
                ),
            )

        self._write(prepare)

    def status(self, manifest):
        return {
            "phase": self._write(
                lambda connection: self._current(connection, manifest)
            ),
            "migration_id": manifest["migration_id"],
        }

    def copy_page(self, manifest, stream, index, records):
        page_digest = digest(records)

        def copy(connection):
            phase = self._current(connection, manifest)
            prior = connection.execute(
                "SELECT digest FROM sonder_child.migration_page WHERE stream=%s AND page=%s",
                (stream, index),
            ).fetchone()
            if prior:
                if prior[0] != page_digest:
                    raise MigrationRefused("migration page identity conflict")
                return
            expected = connection.execute(
                "SELECT count(*) FROM sonder_child.migration_page WHERE stream=%s",
                (stream,),
            ).fetchone()[0]
            if phase != "COPYING" or expected != index:
                raise MigrationRefused("migration page is out of order")
            for row in records:
                validate_record(stream, row)
                if stream == "children":
                    child = session_from_data(json.loads(unbinary(row["snapshot"])))
                    connection.execute(
                        "INSERT INTO sonder_child.child(position,child_id,status,revision,snapshot) VALUES(%s,%s,%s,%s,%s)",
                        (
                            row["position"],
                            row["key"],
                            child.status.value,
                            child.revision,
                            unbinary(row["snapshot"]),
                        ),
                    )
                    connection.execute(
                        "INSERT INTO sonder_child.child_lock VALUES(%s) ON CONFLICT DO NOTHING",
                        (row["key"],),
                    )
                elif stream == "intents":
                    connection.execute(
                        "INSERT INTO sonder_child.intent VALUES(%s,%s,%s,%s,%s,%s)",
                        (
                            row["position"],
                            row["key"],
                            row["child_id"],
                            row["kind"],
                            row["digest"],
                            unbinary(row["payload"]),
                        ),
                    )
                    connection.execute(
                        "INSERT INTO sonder_child.child_lock VALUES(%s) ON CONFLICT DO NOTHING",
                        (row["child_id"],),
                    )
                else:
                    connection.execute(
                        "INSERT INTO sonder_child.receipt VALUES(%s,%s,%s,%s)",
                        (
                            row["key"],
                            row["disposition"],
                            unbinary(row["result"]),
                            row["revision"],
                        ),
                    )
            connection.execute(
                "INSERT INTO sonder_child.migration_page VALUES(%s,%s,%s,%s,%s)",
                (stream, index, page_digest, len(records), records[-1]["position"]),
            )

        self._write(copy)

    def copied(self, manifest):
        def copied(connection):
            if self._current(connection, manifest) != "COPYING":
                return
            for name, key in (
                ("child_position_seq", "children_high_water"),
                ("intent_position_seq", "intents_high_water"),
            ):
                value = manifest["source"][key]
                connection.execute(
                    "SELECT setval(%s::regclass,%s,%s)",
                    ("sonder_child." + name, max(1, value), value > 0),
                )
            connection.execute(
                "UPDATE sonder_child.migration SET phase='COPIED' WHERE id=1"
            )

        self._write(copied)

    def verified(self, manifest):
        def verified(connection):
            phase = self._current(connection, manifest)
            if phase in ("VERIFIED", "ACTIVE"):
                return
            if phase not in ("COPIED", "VERIFIED"):
                raise MigrationRefused("migration target is not ready for verification")
            connection.execute(
                "UPDATE sonder_child.migration SET phase='VERIFIED' WHERE id=1"
            )

        self._write(verified)

    def activate(self, manifest, guard):
        from ...application.subagents.child_migration_activation import (
            require_host_guard,
        )

        require_host_guard(guard, manifest)

        def activate(connection):
            if self._current(connection, manifest) not in ("VERIFIED", "ACTIVE"):
                raise MigrationRefused("migration target is not verified")
            require_host_guard(guard, manifest)
            owner = connection.execute(
                "SELECT owner_id,clean FROM sonder_child.owner WHERE id=1 FOR UPDATE"
            ).fetchone()
            if owner is None:
                connection.execute(
                    "INSERT INTO sonder_child.owner VALUES(1,%s,%s,true)",
                    (self.config.owner_id, "migration-" + manifest["migration_id"]),
                )
            elif owner != (self.config.owner_id, True):
                raise MigrationRefused(
                    "target owner is not eligible for migration activation"
                )
            connection.execute(
                "UPDATE sonder_child.migration SET phase='ACTIVE' WHERE id=1"
            )
            # Every retry emits fresh WAL. A prior local ACTIVE row cannot prove
            # that its remote_apply commit returned before the deadline.
            connection.execute(
                "UPDATE sonder_child.meta SET barrier=barrier+1 WHERE id=1"
            )

        self._write(activate)
        require_host_guard(guard, manifest)

    def retire(self, manifest, guard):
        from ...application.subagents.child_migration_activation import (
            require_host_guard,
        )

        require_host_guard(guard, manifest)

        def retire(connection):
            require_host_guard(guard, manifest)
            if (
                connection.execute(
                    "SELECT to_regclass('sonder_child.migration')"
                ).fetchone()[0]
                is None
            ):
                raise MigrationRefused("source was not created by this migration host")
            owner = connection.execute(
                "SELECT owner_id,incarnation,clean FROM sonder_child.owner WHERE id=1 FOR UPDATE"
            ).fetchone()
            retired_incarnation = "retired-" + manifest["migration_id"]
            if (
                owner is None
                or owner[0] != self.config.owner_id
                or (not owner[2] and owner[1] != retired_incarnation)
            ):
                raise MigrationRefused("source owner retirement identity is unproven")
            # Older PostgreSQL adapters already refuse unclean owners. Preserve
            # that barrier as well as the phase table old code does not read.
            connection.execute(
                "UPDATE sonder_child.owner SET incarnation=%s,clean=false WHERE id=1",
                (retired_incarnation,),
            )
            connection.execute(
                "UPDATE sonder_child.migration SET phase='RETIRED' WHERE id=1"
            )
            connection.execute(
                "UPDATE sonder_child.meta SET barrier=barrier+1 WHERE id=1"
            )

        self._write(retire)
        require_host_guard(guard, manifest)
