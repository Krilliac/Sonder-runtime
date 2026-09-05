"""Atomic owner lifecycle journal. It contains no executable task queue."""

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3

from ...application.ports.runtime_owner import (
    PreparedOwnerOperation,
    OwnerRefused,
    OwnerCommitAmbiguous,
    canonical,
)


class SQLiteRuntimeOwnerJournal:
    MAX_OPERATIONS = 1024

    def __init__(self, path, *, namespace, create=False):
        self.path = Path(path).absolute()
        if not isinstance(namespace, str) or not 1 <= len(namespace) <= 64:
            raise OwnerRefused("invalid owner namespace")
        self.namespace = namespace
        if create:
            # Exclusive filesystem creation distinguishes enrollment from adoption.
            with self.path.open("xb"):
                pass
            with self._transaction() as connection:
                connection.execute(
                    "CREATE TABLE owner(id INTEGER PRIMARY KEY CHECK(id=1),namespace TEXT NOT NULL,revision INTEGER NOT NULL,state TEXT NOT NULL,pending TEXT,selection TEXT)"
                )
                connection.execute(
                    "CREATE TABLE operation(id TEXT PRIMARY KEY,digest TEXT NOT NULL,action TEXT NOT NULL,revision INTEGER NOT NULL,payload BLOB NOT NULL,result BLOB)"
                )
                connection.execute(
                    "CREATE TABLE config(digest TEXT PRIMARY KEY,payload BLOB NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO owner VALUES(1,?,0,'STOPPED_CLEAN',NULL,NULL)",
                    (namespace,),
                )
                connection.execute("PRAGMA user_version=1")
        self.status()

    @contextmanager
    def _transaction(self, command=None):
        if not self.path.is_file():
            raise OwnerRefused("owner journal is missing")
        connection = sqlite3.connect(
            self.path.as_uri() + "?mode=rw", uri=True, timeout=1, isolation_level=None
        )
        try:
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except sqlite3.Error:
            if command is not None:
                raise OwnerCommitAmbiguous(command) from None
            raise OwnerRefused("owner storage is unavailable") from None
        except BaseException:
            raise
        finally:
            connection.close()

    def _owner(self, connection):
        if connection.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise OwnerRefused("unsupported owner journal schema")
        row = connection.execute(
            "SELECT namespace,revision,state,pending,selection FROM owner WHERE id=1"
        ).fetchone()
        if row is None or row[0] != self.namespace:
            raise OwnerRefused("owner namespace does not match")
        return row

    def status(self):
        with self._transaction() as connection:
            row = self._owner(connection)
            return dict(
                namespace=row[0],
                revision=row[1],
                state=row[2],
                pending=row[3],
                selection=row[4],
            )

    def selected_config(self):
        with self._transaction() as connection:
            owner = self._owner(connection)
            row = connection.execute(
                "SELECT payload FROM config WHERE digest=?", (owner[4],)
            ).fetchone()
            if row is None:
                raise OwnerRefused("no immutable owner configuration selected")
            if hashlib.sha256(row[0]).hexdigest() != owner[4]:
                raise OwnerRefused("immutable owner configuration changed")
            return json.loads(row[0])

    def pending(self):
        with self._transaction() as connection:
            owner = self._owner(connection)
            if owner[3] is None:
                return None
            row = connection.execute(
                "SELECT id,action,revision,payload FROM operation WHERE id=? AND result IS NULL",
                (owner[3],),
            ).fetchone()
            if row is None:
                raise OwnerRefused("pending owner operation is missing")
            return PreparedOwnerOperation(*row)

    @staticmethod
    def _existing(connection, command):
        row = connection.execute(
            "SELECT digest,result FROM operation WHERE id=?", (command.operation_id,)
        ).fetchone()
        if row is not None and row[0] != command.digest:
            raise OwnerRefused("owner operation identity conflict")
        return row

    def prepare(self, command):
        if type(command) is not PreparedOwnerOperation:
            raise OwnerRefused("immutable prepared owner command required")
        with self._transaction(command) as connection:
            owner = self._owner(connection)
            prior = self._existing(connection, command)
            if prior is not None:
                return None if prior[1] is None else json.loads(prior[1])
            if owner[1] != command.expected_revision or owner[3] is not None:
                raise OwnerRefused("owner revision conflict or unresolved operation")
            eligible = {
                "select": {"STOPPED_CLEAN"},
                "launch": {"STOPPED_CLEAN"},
                "stop": {"RUNNING"},
            }
            if owner[2] not in eligible[command.action] or (
                command.action == "launch" and owner[4] is None
            ):
                raise OwnerRefused("owner state does not permit this operation")
            if (
                connection.execute("SELECT count(*) FROM operation").fetchone()[0]
                >= self.MAX_OPERATIONS
            ):
                raise OwnerRefused(
                    "owner receipt capacity exhausted; retained history preserved"
                )
            connection.execute(
                "INSERT INTO operation VALUES(?,?,?,?,?,NULL)",
                (
                    command.operation_id,
                    command.digest,
                    command.action,
                    command.expected_revision,
                    command.payload,
                ),
            )
            connection.execute(
                "UPDATE owner SET revision=revision+1,pending=? WHERE id=1",
                (command.operation_id,),
            )
            return None

    def complete(self, command, result, state):
        encoded = canonical(result)
        if type(result) is not dict or len(encoded) > 32768:
            raise OwnerRefused("owner result exceeds bounds")
        with self._transaction(command) as connection:
            owner = self._owner(connection)
            prior = self._existing(connection, command)
            if prior is not None and prior[1] is not None:
                return json.loads(prior[1])
            if (
                prior is None
                or owner[3] != command.operation_id
                or owner[1] != command.expected_revision + 1
            ):
                raise OwnerRefused("exact pending owner operation required")
            allowed = {
                "select": {"STOPPED_CLEAN"},
                "launch": {"RUNNING"},
                "stop": {"STOPPED_CLEAN", "STOPPED_UNCLEAN"},
            }
            if state not in allowed[command.action]:
                raise OwnerRefused("invalid owner completion state")
            selection = owner[4]
            if command.action == "select":
                config = canonical(json.loads(command.payload)["config"])
                selection = hashlib.sha256(config).hexdigest()
                connection.execute(
                    "INSERT OR IGNORE INTO config VALUES(?,?)", (selection, config)
                )
            receipt = canonical(
                {
                    "operation_id": command.operation_id,
                    "revision": owner[1] + 1,
                    "state": state,
                    "result": result,
                }
            )
            connection.execute(
                "UPDATE operation SET result=? WHERE id=?",
                (receipt, command.operation_id),
            )
            connection.execute(
                "UPDATE owner SET revision=revision+1,state=?,pending=NULL,selection=? WHERE id=1",
                (state, selection),
            )
            return json.loads(receipt)
