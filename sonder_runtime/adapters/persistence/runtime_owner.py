"""Atomic owner lifecycle journal. It contains no executable task queue."""

from sonder_runtime.adapters.persistence.owned_sqlite import connect as owned_sqlite_connect

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
        connection = owned_sqlite_connect(
            self.path.as_uri() + "?mode=rw", uri=True, timeout=1, isolation_level=None
        )
        try:
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except sqlite3.Error:
            connection.rollback()
            if command is not None:
                raise OwnerCommitAmbiguous(command) from None
            raise OwnerRefused("owner storage is unavailable") from None
        except BaseException:
            connection.rollback()
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
        command = PreparedOwnerOperation(
            command.operation_id,
            command.action,
            command.expected_revision,
            command.payload,
        )
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
        if type(command) is not PreparedOwnerOperation:
            raise OwnerRefused("immutable prepared owner command required")
        command = PreparedOwnerOperation(
            command.operation_id,
            command.action,
            command.expected_revision,
            command.payload,
        )
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
                    "result": json.loads(encoded),
                }
            )
            changed = connection.execute(
                "UPDATE operation SET result=? WHERE id=? AND digest=? AND result IS NULL",
                (receipt, command.operation_id, command.digest),
            ).rowcount
            if changed != 1:
                raise OwnerRefused("exact owner receipt was not retained")
            changed = connection.execute(
                "UPDATE owner SET revision=revision+1,state=?,pending=NULL,selection=? WHERE id=1 AND revision=? AND pending=?",
                (state, selection, owner[1], command.operation_id),
            ).rowcount
            if changed != 1:
                raise OwnerRefused("owner state advancement conflicted")
            return json.loads(receipt)


class SQLiteManagedRuntimeOwnerJournal(SQLiteRuntimeOwnerJournal):
    """Schema-2 lifecycle writer in the same canonical owner.sqlite aggregate.

    Schema-1 prototype readers refuse this namespace. Reopening the journal is
    observation only; a journal object does not grant process/launch authority.
    """
    def __init__(self, path, *, namespace, create=False):
        import re
        import uuid
        self.path = Path(path).absolute()
        if type(namespace) is not str or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", namespace) is None:
            raise OwnerRefused("invalid managed namespace")
        self.namespace = namespace
        if create:
            with self.path.open("xb"):
                pass
            with self._transaction() as connection:
                schema = """
                    CREATE TABLE owner(id INTEGER PRIMARY KEY CHECK(id=1),namespace TEXT NOT NULL,revision INTEGER NOT NULL,state TEXT NOT NULL,pending TEXT,selection TEXT);
                    CREATE TABLE operation(id TEXT PRIMARY KEY,digest TEXT NOT NULL,action TEXT NOT NULL,revision INTEGER NOT NULL,payload BLOB NOT NULL,result BLOB);
                    CREATE TABLE config(digest TEXT PRIMARY KEY,payload BLOB NOT NULL);
                    CREATE TABLE managed_identity(id INTEGER PRIMARY KEY CHECK(id=1),epoch INTEGER NOT NULL,incarnation TEXT NOT NULL,config_revision INTEGER NOT NULL,selector_revision INTEGER NOT NULL);
                    CREATE TABLE managed_command(id TEXT PRIMARY KEY,namespace TEXT NOT NULL,incarnation TEXT NOT NULL,epoch INTEGER NOT NULL,config_revision INTEGER NOT NULL,selector_revision INTEGER NOT NULL);
                    CREATE TABLE managed_phase(operation_id TEXT NOT NULL,phase TEXT NOT NULL,evidence BLOB NOT NULL,PRIMARY KEY(operation_id,phase));
                """
                # executescript implicitly commits the active transaction.
                # Fixed DDL statements must share the identity insert commit.
                for statement in schema.split(";"):
                    if statement.strip():
                        connection.execute(statement)
                connection.execute("INSERT INTO owner VALUES(1,?,0,'STOPPED_CLEAN',NULL,NULL)", (namespace,))
                connection.execute("INSERT INTO managed_identity VALUES(1,1,?,0,0)", (uuid.uuid4().hex,))
                connection.execute("PRAGMA user_version=2")
        self.status()

    def _owner(self, connection):
        if connection.execute("PRAGMA user_version").fetchone()[0] != 2:
            raise OwnerRefused("managed namespace schema mismatch")
        row = connection.execute("SELECT namespace,revision,state,pending,selection FROM owner WHERE id=1").fetchone()
        if row is None or row[0] != self.namespace:
            raise OwnerRefused("managed namespace identity mismatch")
        return row

    def status(self):
        with self._transaction() as connection:
            owner = self._owner(connection)
            identity = connection.execute("SELECT epoch,incarnation,config_revision,selector_revision FROM managed_identity WHERE id=1").fetchone()
            if identity is None:
                raise OwnerRefused("managed incarnation is missing")
            return dict(namespace=owner[0], revision=owner[1], state=owner[2], pending=owner[3], selection=owner[4], epoch=identity[0], incarnation=identity[1], config_revision=identity[2], selector_revision=identity[3])

    @staticmethod
    def _command(command):
        from ...application.ports.managed_runtime_owner import PreparedManagedOwnerOperation
        if type(command) is not PreparedManagedOwnerOperation:
            raise OwnerRefused("exact immutable managed operation required")
        return PreparedManagedOwnerOperation(command.operation_id, command.action, command.namespace, command.incarnation, command.expected_revision, command.epoch, command.config_revision, command.selector_revision, command.payload)

    def pending(self):
        from ...application.ports.managed_runtime_owner import PreparedManagedOwnerOperation
        with self._transaction() as connection:
            owner = self._owner(connection)
            if owner[3] is None:
                return None
            row = connection.execute("SELECT o.id,o.action,m.namespace,m.incarnation,o.revision,m.epoch,m.config_revision,m.selector_revision,o.payload FROM operation o JOIN managed_command m ON m.id=o.id WHERE o.id=?", (owner[3],)).fetchone()
            if row is None:
                raise OwnerRefused("pending managed operation is missing")
            return PreparedManagedOwnerOperation(*row)

    def prepare(self, command):
        command = self._command(command)
        with self._transaction(command) as connection:
            owner = self._owner(connection)
            prior = self._existing(connection, command)
            if prior is not None:
                return None if prior[1] is None else json.loads(prior[1])
            identity = connection.execute("SELECT incarnation,epoch,config_revision,selector_revision FROM managed_identity WHERE id=1").fetchone()
            if owner[0] != command.namespace or owner[1] != command.expected_revision or owner[3] is not None or identity != (command.incarnation, command.epoch, command.config_revision, command.selector_revision):
                raise OwnerRefused("managed owner generation conflict or unresolved operation")
            eligible = {"select": "STOPPED_CLEAN", "launch": "STOPPED_CLEAN", "stop": "RUNNING", "activate": "STOPPED_CLEAN"}
            if owner[2] != eligible[command.action] or command.action in {"launch", "activate"} and owner[4] is None:
                raise OwnerRefused("managed state does not admit this operation")
            if connection.execute("SELECT count(*) FROM operation").fetchone()[0] >= self.MAX_OPERATIONS:
                raise OwnerRefused("managed receipt capacity exhausted; history preserved")
            payload = json.loads(command.payload)
            if command.action in {"select", "activate"}:
                target = payload["config" if command.action == "select" else "target"]
                expected_selector = command.selector_revision + (command.action == "activate")
                if target["generation"] != command.config_revision + 1 or target["selector_revision"] != expected_selector:
                    raise OwnerRefused("managed config/selector generation conflict")
            connection.execute("INSERT INTO operation VALUES(?,?,?,?,?,NULL)", (command.operation_id, command.digest, command.action, command.expected_revision, command.payload))
            connection.execute("INSERT INTO managed_command VALUES(?,?,?,?,?,?)", (command.operation_id, command.namespace, command.incarnation, command.epoch, command.config_revision, command.selector_revision))
            state = {"select": "CONFIG_PREPARED", "launch": "LAUNCH_PREPARED", "stop": "QUIESCING", "activate": "MIGRATING"}[command.action]
            connection.execute("UPDATE owner SET revision=revision+1,state=?,pending=? WHERE id=1", (state, command.operation_id))
            return None

    def _pending(self, connection, command):
        owner = self._owner(connection)
        prior = self._existing(connection, command)
        if prior is None or prior[1] is not None or owner[3] != command.operation_id:
            raise OwnerRefused("exact pending managed operation required")
        identity = connection.execute("SELECT incarnation,epoch,config_revision,selector_revision FROM managed_identity WHERE id=1").fetchone()
        if owner[0] != command.namespace or identity != (command.incarnation, command.epoch, command.config_revision, command.selector_revision):
            raise OwnerRefused("managed owner incarnation changed")
        return owner

    def phase(self, command, phase, evidence):
        command = self._command(command)
        allowed = {"select": set(), "launch": {"STARTING", "LAUNCH_UNKNOWN"}, "stop": {"QUIESCE_UNKNOWN"}, "activate": {"ACTIVATION_INCOMPLETE"}}
        encoded = canonical(evidence)
        if phase not in allowed[command.action] or type(evidence) is not dict or len(encoded) > 32768:
            raise OwnerRefused("invalid bounded managed phase")
        with self._transaction(command) as connection:
            self._pending(connection, command)
            old = connection.execute("SELECT evidence FROM managed_phase WHERE operation_id=? AND phase=?", (command.operation_id, phase)).fetchone()
            if old is not None:
                if old[0] != encoded:
                    raise OwnerRefused("managed phase identity conflict")
                return
            connection.execute("INSERT INTO managed_phase VALUES(?,?,?)", (command.operation_id, phase, encoded))
            connection.execute("UPDATE owner SET revision=revision+1,state=? WHERE id=1 AND pending=?", (phase, command.operation_id))

    def phase_evidence(self, command, phase):
        command = self._command(command)
        with self._transaction() as connection:
            self._owner(connection)
            self._existing(connection, command)
            row = connection.execute("SELECT evidence FROM managed_phase WHERE operation_id=? AND phase=?", (command.operation_id, phase)).fetchone()
            return None if row is None else json.loads(row[0])

    def complete(self, command, result, state):
        command = self._command(command)
        encoded = canonical(result)
        if type(result) is not dict or len(encoded) > 32768:
            raise OwnerRefused("managed result exceeds bounds")
        allowed = {"select": {"STOPPED_CLEAN"}, "launch": {"RUNNING"}, "stop": {"STOPPED_CLEAN", "STOPPED_UNCLEAN"}, "activate": {"STOPPED_CLEAN"}}
        if state not in allowed[command.action]:
            raise OwnerRefused("invalid managed completion state")
        with self._transaction(command) as connection:
            prior = self._existing(connection, command)
            if prior is not None and prior[1] is not None:
                return json.loads(prior[1])
            owner = self._pending(connection, command)
            selection, generation, selector = owner[4], command.config_revision, command.selector_revision
            if command.action in {"select", "activate"}:
                config = json.loads(command.payload)["config" if command.action == "select" else "target"]
                if command.action == "activate" and result.get("phase") != "COMPLETE":
                    raise OwnerRefused("activation completion proof required")
                payload = canonical(config)
                selection = hashlib.sha256(payload).hexdigest()
                connection.execute("INSERT OR IGNORE INTO config VALUES(?,?)", (selection, payload))
                generation, selector = config["generation"], config["selector_revision"]
            receipt = canonical(dict(operation_id=command.operation_id, revision=owner[1]+1, state=state, epoch=command.epoch, config_revision=generation, selector_revision=selector, result=json.loads(encoded)))
            changed = connection.execute("UPDATE operation SET result=? WHERE id=? AND digest=? AND result IS NULL", (receipt, command.operation_id, command.digest)).rowcount
            if changed != 1:
                raise OwnerRefused("managed receipt was not retained")
            changed = connection.execute("UPDATE owner SET revision=revision+1,state=?,pending=NULL,selection=? WHERE id=1 AND revision=? AND pending=?", (state, selection, owner[1], command.operation_id)).rowcount
            if changed != 1:
                raise OwnerRefused("managed completion conflicted")
            connection.execute("UPDATE managed_identity SET config_revision=?,selector_revision=? WHERE id=1", (generation, selector))
            return json.loads(receipt)
