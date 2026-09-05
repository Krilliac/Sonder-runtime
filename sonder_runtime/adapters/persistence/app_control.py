"""Durable app-control metadata on the existing private fleet database."""

from sonder_runtime.adapters.persistence.owned_sqlite import (
    connect as owned_sqlite_connect,
)

from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import time
import uuid
from ...application.ports.lane_continuation import (
    PendingVerificationIdentity,
    PendingApprovalEvidence,
    TerminalProjectionReceipt,
)
from ...application.ports.host_turn_links import (
    ManagedHostTurnLink,
    ManagedHostTerminalLink,
)
from ...application.ports.app_managed_work import (
    AppWorkRecord,
    PreparedAppWork,
    PreparedWorkbenchRun,
    WorkSpec,
    WorkAdmission,
    WorkInterruption,
    WorkCompletionEvidence,
    WorkVerificationPending,
)

from ...application.ports.app_control import (
    AppControlError,
    AppControlLimits,
    BindingPage,
    BindingRecord,
    CapacityExceeded,
    CommandConflict,
    CommandKey,
    CommandReceipt,
    CommandRecord,
    ControlSessionRecord,
    GrantSnapshot,
    NotFound,
    OutcomeUnknown,
    SelectionRecord,
    StoreUnavailable,
    digest,
    identifier,
    principal,
    positive,
)

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS app_managed_work (position INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL, principal TEXT NOT NULL, session TEXT NOT NULL, binding TEXT NOT NULL, state TEXT NOT NULL, revision INTEGER NOT NULL, record TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS app_managed_work_scope ON app_managed_work(principal,session,position)",
    "CREATE UNIQUE INDEX IF NOT EXISTS app_managed_work_active_binding ON app_managed_work(binding) WHERE state IN ('prepared','admitted')",
    "CREATE UNIQUE INDEX IF NOT EXISTS app_managed_work_active_binding_v2 ON app_managed_work(binding) WHERE state != 'terminal'",
    "CREATE TABLE IF NOT EXISTS app_control_meta (id INTEGER PRIMARY KEY CHECK(id=1), identity TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS app_control_sessions (id TEXT PRIMARY KEY, principal TEXT NOT NULL, runtime TEXT NOT NULL, record TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS app_control_sessions_principal ON app_control_sessions(principal)",
    "CREATE TABLE IF NOT EXISTS app_host_bindings (position INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL, host TEXT UNIQUE NOT NULL, principal TEXT NOT NULL, runtime TEXT NOT NULL, record TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS app_host_bindings_principal ON app_host_bindings(principal,position)",
    "CREATE TABLE IF NOT EXISTS app_control_selections (principal TEXT NOT NULL, session TEXT NOT NULL REFERENCES app_control_sessions(id), record TEXT NOT NULL, PRIMARY KEY(principal,session))",
    "CREATE TABLE IF NOT EXISTS app_control_commands (principal TEXT NOT NULL, scope TEXT NOT NULL, id TEXT NOT NULL, action TEXT NOT NULL, digest TEXT NOT NULL, receipt TEXT NOT NULL, PRIMARY KEY(principal,scope,id))",
    "CREATE TABLE IF NOT EXISTS app_control_grant_revisions (runtime TEXT NOT NULL, grant_id TEXT NOT NULL, revision INTEGER NOT NULL, digest TEXT NOT NULL, source_digest TEXT NOT NULL, PRIMARY KEY(runtime,grant_id))",
)

_CONTROL_TABLES = (
    "app_managed_work",
    "app_control_meta",
    "app_control_sessions",
    "app_host_bindings",
    "app_control_selections",
    "app_control_commands",
    "app_control_grant_revisions",
)


def _encode(value, limit=131072):
    data = asdict(value) if not isinstance(value, dict) else value
    if type(value) is AppWorkRecord:
        # Preserve exact canonical bytes for pre-execution records already stored.
        if not value.run_id:
            data.pop("run_id")
        if value.host_turn is None:
            data.pop("host_turn")
        if value.interruption is None:
            data.pop("interruption")
        if value.terminal is None:
            data.pop("terminal")
        if value.completion is None:
            data.pop("completion")
        if value.verification_pending is None:
            data.pop("verification_pending")
    raw = json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    if len(raw.encode("utf8")) > limit:
        raise CapacityExceeded("control record exceeds byte bound")
    return raw


def _pairs(pairs):
    data = {}
    for k, v in pairs:
        if k in data:
            raise StoreUnavailable("duplicate stored key")
        data[k] = v
    return data


def _decode(raw, cls):
    try:
        if type(raw) is not str or len(raw.encode("utf8")) > 131072:
            raise ValueError()
        data = json.loads(raw, object_pairs_hook=_pairs)
        if cls is AppWorkRecord:
            if data.get("verification_pending") is not None:
                pending = data["verification_pending"]
                pending["identity"] = PendingVerificationIdentity(**pending["identity"])
                pending["approval"] = PendingApprovalEvidence(**pending["approval"])
                pending["original_terminal"]["turn"] = ManagedHostTurnLink(
                    **pending["original_terminal"]["turn"]
                )
                pending["original_terminal"] = ManagedHostTerminalLink(
                    **pending["original_terminal"]
                )
                data["verification_pending"] = WorkVerificationPending(**pending)
            if data.get("completion") is not None:
                completion = data["completion"]
                if completion.get("pending_identity") is not None:
                    completion["pending_identity"] = PendingVerificationIdentity(
                        **completion["pending_identity"]
                    )
                if completion.get("publication_receipt") is not None:
                    completion["publication_receipt"] = TerminalProjectionReceipt(
                        **completion["publication_receipt"]
                    )
                data["completion"] = WorkCompletionEvidence(**completion)
            if data.get("terminal") is not None:
                data["terminal"]["turn"] = ManagedHostTurnLink(
                    **data["terminal"]["turn"]
                )
                data["terminal"] = ManagedHostTerminalLink(**data["terminal"])
            if data.get("interruption") is not None:
                data["interruption"] = WorkInterruption(**data["interruption"])
            if data.get("host_turn") is not None:
                data["host_turn"] = ManagedHostTurnLink(**data["host_turn"])
            prepared = data["prepared"]
            grant = prepared["binding"]["grant"]
            for key in ("roots", "tools", "catalog_file_identity"):
                grant[key] = tuple(grant[key])
            prepared["binding"]["grant"] = GrantSnapshot(**grant)
            prepared["binding"] = BindingRecord(**prepared["binding"])
            prepared["selection"] = SelectionRecord(**prepared["selection"])
            prepared["command"] = CommandKey(**prepared["command"])
            prepared["plan"]["spec"] = WorkSpec(**prepared["plan"]["spec"])
            prepared["plan"]["model_ladder"] = tuple(prepared["plan"]["model_ladder"])
            prepared["plan"] = PreparedWorkbenchRun(**prepared["plan"])
            data["prepared"] = PreparedAppWork(**prepared)
        if cls in (BindingRecord, ControlSessionRecord):
            grant = data["grant"]
            for key in ("roots", "tools", "catalog_file_identity"):
                grant[key] = tuple(grant[key])
            data["grant"] = GrantSnapshot(**grant)
        value = cls(**data)
        if _encode(value) != raw:
            raise ValueError()
        return value
    except Exception:
        raise StoreUnavailable("invalid stored control record") from None


def _authority_digest(grant):
    data = asdict(grant)
    data.pop("catalog_digest")
    data.pop("catalog_file_identity")
    return hashlib.sha256(_encode(data).encode()).hexdigest()


def initialize_schema(conn):
    """Caller owns an idle connection to an existing fleet database."""
    if conn.in_transaction:
        raise StoreUnavailable("schema initialization requires idle connection")
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fleet_agents'"
    ).fetchone():
        raise StoreUnavailable("existing fleet schema required")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("BEGIN IMMEDIATE")
    try:
        for statement in _SCHEMA:
            conn.execute(statement)
        conn.execute(
            "INSERT OR IGNORE INTO app_control_meta VALUES (1,?)", (uuid.uuid4().hex,)
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


class SQLiteAppControlStore:
    def __init__(self, path, *, limits=AppControlLimits(), clock=time.time):
        raw = Path(path)
        if not raw.is_absolute() or str(raw) != str(raw.resolve()):
            raise StoreUnavailable("canonical fleet path required")
        if type(limits) is not AppControlLimits or not callable(clock):
            raise ValueError("typed limits and clock required")
        self.path = str(raw)
        self.limits = limits
        self.clock = clock
        self._file_identity = self._stat()
        conn = self._connect()
        try:
            initialize_schema(conn)
            self._identity = conn.execute(
                "SELECT identity FROM app_control_meta WHERE id=1"
            ).fetchone()[0]
        finally:
            conn.close()

    def _stat(self):
        path = Path(self.path)
        if str(path.parent) != str(path.parent.resolve()) or path.is_symlink():
            raise StoreUnavailable("fleet source changed")
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or getattr(info, "st_file_attributes", 0) & 0x400
        ):
            raise StoreUnavailable("invalid fleet source")
        for suffix in ("-wal", "-shm", "-journal"):
            side = Path(self.path + suffix)
            if side.exists() or side.is_symlink():
                meta = side.lstat()
                if (
                    not stat.S_ISREG(meta.st_mode)
                    or meta.st_nlink != 1
                    or getattr(meta, "st_file_attributes", 0) & 0x400
                    or side.is_symlink()
                ):
                    raise StoreUnavailable("invalid fleet sidecar")
        return (info.st_dev, info.st_ino)

    def _connect(self):
        conn = owned_sqlite_connect(
            Path(self.path).as_uri() + "?mode=rw", uri=True, timeout=5
        )
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def _validate(self, conn):
        # A borrowed connection must not redirect unqualified control queries
        # through caller-owned temporary tables or views.
        placeholders = ",".join("?" for _ in _CONTROL_TABLES)
        shadow = conn.execute(
            f"SELECT 1 FROM sqlite_temp_master WHERE lower(name) IN ({placeholders}) "
            f"OR (type='trigger' AND lower(tbl_name) IN ({placeholders})) LIMIT 1",
            _CONTROL_TABLES + _CONTROL_TABLES,
        ).fetchone()
        if shadow:
            raise StoreUnavailable("shadowed control schema refused")
        if conn.execute(
            f"SELECT 1 FROM main.sqlite_master WHERE type='trigger' "
            f"AND lower(tbl_name) IN ({placeholders}) LIMIT 1",
            _CONTROL_TABLES,
        ).fetchone():
            raise StoreUnavailable("unexpected control schema trigger refused")
        databases = conn.execute("PRAGMA database_list").fetchall()
        main = next((row[2] for row in databases if row[1] == "main"), None)
        if (
            main is None
            or Path(main).resolve() != Path(self.path)
            or self._stat() != self._file_identity
        ):
            raise StoreUnavailable("fleet connection identity mismatch")
        row = conn.execute(
            "SELECT identity FROM app_control_meta WHERE id=1"
        ).fetchone()
        if row is None or row[0] != self._identity:
            raise StoreUnavailable("fleet schema identity mismatch")
        if (
            conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1
            or conn.execute("PRAGMA synchronous").fetchone()[0] != 2
        ):
            raise StoreUnavailable("fleet connection durability required")

    def _commit(self, conn):
        conn.commit()

    def atomic(self, callback, *, connection=None):
        owned = connection is None
        conn = self._connect() if owned else connection
        tx = None
        committing = False
        try:
            if owned:
                conn.execute("BEGIN IMMEDIATE")
            elif not conn.in_transaction:
                raise StoreUnavailable("borrowed connection must own a transaction")
            self._validate(conn)
            tx = AppControlTransaction(conn, self)
            result = callback(tx)
            self._validate(conn)
            if owned:
                committing = True
                self._commit(conn)
                self._validate(conn)
            return result
        except BaseException as error:
            if owned:
                try:
                    conn.rollback()
                except Exception:
                    pass
            if committing:
                raise OutcomeUnknown("control commit outcome unknown") from None
            if isinstance(error, (AppControlError, ValueError, TypeError)):
                raise
            raise StoreUnavailable("control store unavailable") from None
        finally:
            if tx is not None:
                tx._active = False
            if owned:
                conn.close()


class AppControlTransaction:
    def __init__(self, conn, store):
        self._conn = conn
        self._store = store
        self._active = True

    def _check(self):
        if not self._active or not self._conn.in_transaction:
            raise StoreUnavailable("control transaction is no longer active")

    def read_work(self, *, principal_id, control_session_id, work_id):
        self._check()
        principal(principal_id)
        identifier(control_session_id)
        identifier(work_id)
        row = self._conn.execute(
            "SELECT id,principal,session,binding,state,revision,record FROM app_managed_work "
            "WHERE id=? AND principal=? AND session=?",
            (work_id, principal_id, control_session_id),
        ).fetchone()
        if row is None:
            return None
        record = _decode(row[6], AppWorkRecord)
        work = record.prepared
        expected = (
            work.work_id,
            work.command.principal_id,
            work.selection.control_session_id,
            work.binding.binding_id,
            record.state,
            record.revision,
        )
        if tuple(row[:6]) != expected:
            raise StoreUnavailable("work row scope mismatch")
        return record

    def _require_work(self, work):
        if type(work) is not PreparedAppWork:
            raise ValueError("typed prepared work required")
        work.__post_init__()
        session, binding, selection = self.require_selection(
            principal_id=work.command.principal_id,
            control_session_id=work.selection.control_session_id,
            binding_id=work.binding.binding_id,
            binding_revision=work.binding.revision,
            selection_id=work.selection.selection_id,
            epoch=work.selection.epoch,
        )
        if (
            binding != work.binding
            or selection != work.selection
            or session.account_session_ref != work.account_session_ref
            or session.grant != binding.grant
            or not work.created_at
            <= self._now()
            < work.expires_at
            <= session.expires_at
        ):
            raise CommandConflict("prepared work authority changed or expired")

    def prepare_work(self, work):
        self._require_work(work)
        prior = self._start(work.command, "prepare_work", work.digest)
        if prior:
            expected = CommandReceipt(
                work.command.command_id,
                "prepare_work",
                "COMMITTED",
                work.work_id,
                1,
                work.selection.epoch,
            )
            if prior != expected:
                raise StoreUnavailable("prepared work receipt mismatch")
            result = self.read_work(
                principal_id=work.command.principal_id,
                control_session_id=work.selection.control_session_id,
                work_id=prior.entity_id,
            )
            if result is None or result.prepared != work:
                raise StoreUnavailable("prepared work receipt mismatch")
            return result
        retained = self._conn.execute(
            "SELECT principal,session,id FROM app_managed_work WHERE binding=? LIMIT 257",
            (work.binding.binding_id,),
        ).fetchall()
        if len(retained) > 256:
            raise CapacityExceeded("retained work capacity exceeded")
        for owner, session, work_id in retained:
            previous = self.read_work(
                principal_id=owner, control_session_id=session, work_id=work_id
            )
            if (
                previous is None
                or previous.prepared.binding.binding_id != work.binding.binding_id
                or owner != work.binding.principal_id
            ):
                raise StoreUnavailable("retained binding work mismatch")
            if previous.state != "terminal":
                raise CommandConflict("binding already has active work")
        if (
            self._conn.execute("SELECT COUNT(*) FROM app_managed_work").fetchone()[0]
            >= 256
        ):
            raise CapacityExceeded("retained work capacity reached")
        record = AppWorkRecord(work)
        self._write_one(
            "INSERT INTO app_managed_work(id,principal,session,binding,state,revision,record) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                work.work_id,
                work.command.principal_id,
                work.selection.control_session_id,
                work.binding.binding_id,
                record.state,
                record.revision,
                _encode(record),
            ),
        )
        if (
            self.read_work(
                principal_id=work.command.principal_id,
                control_session_id=work.selection.control_session_id,
                work_id=work.work_id,
            )
            != record
        ):
            raise StoreUnavailable("work preparation readback mismatch")
        self._finish(
            work.command,
            work.digest,
            CommandReceipt(
                work.command.command_id,
                "prepare_work",
                "COMMITTED",
                work.work_id,
                1,
                work.selection.epoch,
            ),
        )
        return record

    def admit_work(
        self,
        *,
        principal_id,
        control_session_id,
        work_id,
        expected_revision,
        dispatch_id,
        process_incarnation,
    ):
        positive(expected_revision)
        identifier(dispatch_id)
        identifier(process_incarnation)
        record = self.read_work(
            principal_id=principal_id,
            control_session_id=control_session_id,
            work_id=work_id,
        )
        if record is None:
            raise NotFound("work unavailable")
        self._require_work(record.prepared)
        work = record.prepared
        preparation = self.command(
            work.command, action="prepare_work", argument_digest=work.digest
        )
        expected_receipt = CommandReceipt(
            work.command.command_id,
            "prepare_work",
            "COMMITTED",
            work.work_id,
            1,
            work.selection.epoch,
        )
        if preparation is None or preparation.public_receipt != expected_receipt:
            raise StoreUnavailable("prepared work receipt mismatch")
        if record.state != "prepared":
            if (
                expected_revision != 1
                or record.dispatch_id != dispatch_id
                or record.process_incarnation != process_incarnation
            ):
                raise CommandConflict("work was already admitted")
            return WorkAdmission(record, False)
        if expected_revision != record.revision:
            raise CommandConflict("work revision changed")
        admitted = replace(
            record,
            state="admitted",
            revision=2,
            dispatch_id=dispatch_id,
            process_incarnation=process_incarnation,
        )
        self._write_one(
            "UPDATE app_managed_work SET state=?,revision=?,record=? "
            "WHERE id=? AND principal=? AND session=? AND revision=? AND state='prepared'",
            (
                admitted.state,
                admitted.revision,
                _encode(admitted),
                work_id,
                principal_id,
                control_session_id,
                expected_revision,
            ),
        )
        if (
            self.read_work(
                principal_id=principal_id,
                control_session_id=control_session_id,
                work_id=work_id,
            )
            != admitted
        ):
            raise StoreUnavailable("work admission readback mismatch")
        return WorkAdmission(admitted, True)

    def _link_work(
        self,
        *,
        principal_id,
        control_session_id,
        work_id,
        expected_revision,
        dispatch_id,
        process_incarnation,
        run_id=None,
        host_turn=None,
        interruption=None,
        terminal=None,
        completion=None,
        pending=None,
    ):
        positive(expected_revision)
        current = self.read_work(
            principal_id=principal_id,
            control_session_id=control_session_id,
            work_id=work_id,
        )
        if current is None or current.state == "prepared":
            raise CommandConflict("work is not admitted")
        record = self.admit_work(
            principal_id=principal_id,
            control_session_id=control_session_id,
            work_id=work_id,
            expected_revision=1,
            dispatch_id=dispatch_id,
            process_incarnation=process_incarnation,
        ).record
        if record.state == "prepared":
            raise CommandConflict("work is not admitted")
        if pending is not None and record.verification_pending is not None:
            if record.verification_pending == pending and expected_revision == 4:
                return record
            raise CommandConflict("pending verification evidence is immutable")
        if terminal is not None and record.state == "terminal":
            if (
                record.terminal == terminal
                and record.completion == completion
                and record.revision == expected_revision + 1
            ):
                return record
            raise CommandConflict("terminal link is immutable")
        expected_state = (
            interruption.prior_state
            if interruption is not None
            else "admitted" if host_turn is None else "run_binding"
        )
        if pending is not None:
            expected_state = "running"
        if terminal is not None:
            if record.state not in ("running", "unknown", "verification_pending"):
                raise CommandConflict("retained running host required")
            expected_state = record.state
        if record.state != expected_state or record.revision != expected_revision:
            raise CommandConflict("work execution state changed")
        if terminal is not None:
            updated = replace(
                record,
                state="terminal",
                revision=record.revision + 1,
                terminal=terminal,
                completion=completion,
            )
        elif pending is not None:
            if pending.approval.expires_at <= self._now():
                raise CommandConflict("new pending approval observation has expired")
            updated = replace(
                record,
                state="verification_pending",
                revision=5,
                verification_pending=pending,
            )
        elif interruption is not None:
            updated = replace(
                record,
                state="unknown",
                revision=record.revision + 1,
                interruption=interruption,
            )
        elif host_turn is not None:
            updated = replace(record, state="running", revision=4, host_turn=host_turn)
        else:
            updated = replace(record, state="run_binding", revision=3, run_id=run_id)
        self._write_one(
            "UPDATE app_managed_work SET state=?,revision=?,record=? "
            "WHERE id=? AND principal=? AND session=? AND revision=? AND state=?",
            (
                updated.state,
                updated.revision,
                _encode(updated),
                work_id,
                principal_id,
                control_session_id,
                expected_revision,
                expected_state,
            ),
        )
        if (
            self.read_work(
                principal_id=principal_id,
                control_session_id=control_session_id,
                work_id=work_id,
            )
            != updated
        ):
            raise StoreUnavailable("work execution readback mismatch")
        return updated

    def bind_work_run(self, *, run_id, **scope):
        return self._link_work(run_id=run_id, **scope)

    def bind_work_host(self, *, host_turn, **scope):
        if type(host_turn) is not ManagedHostTurnLink:
            raise CommandConflict("typed host turn required")
        return self._link_work(host_turn=host_turn, **scope)

    def mark_work_unknown(self, *, interruption, **scope):
        if type(interruption) is not WorkInterruption:
            raise CommandConflict("typed interruption required")
        interruption.__post_init__()
        return self._link_work(interruption=interruption, **scope)

    def record_work_verification_pending(self, *, pending, **scope):
        if type(pending) is not WorkVerificationPending:
            raise CommandConflict("typed pending verification required")
        pending.__post_init__()
        return self._link_work(pending=pending, **scope)

    def record_work_terminal(self, *, terminal, completion=None, **scope):
        """Record a link validated by private host composition; no authority is minted."""
        if type(terminal) is not ManagedHostTerminalLink:
            raise CommandConflict("typed terminal link required")
        return self._link_work(terminal=terminal, completion=completion, **scope)

    def _now(self):
        self._check()
        return self._store.clock()

    def _write_one(self, statement, parameters):
        self._check()
        self._store._validate(self._conn)
        before = self._conn.total_changes
        cursor = self._conn.execute(statement, parameters)
        if cursor.rowcount != 1 or self._conn.total_changes - before != 1:
            raise StoreUnavailable("control mutation did not affect exactly one row")
        self._store._validate(self._conn)

    def command(self, key, *, action, argument_digest):
        self._check()
        if type(key) is not CommandKey:
            raise ValueError("typed command key required")
        digest(argument_digest)
        row = self._conn.execute(
            "SELECT action,digest,receipt FROM app_control_commands WHERE principal=? AND scope=? AND id=?",
            (key.principal_id, key.session_scope, key.command_id),
        ).fetchone()
        if row is None:
            return None
        if row[0] != action or row[1] != argument_digest:
            raise CommandConflict("immutable command mismatch")
        receipt = _decode(row[2], CommandReceipt)
        return CommandRecord(key, row[0], row[1], "committed", receipt)

    def _start(self, key, action, argument_digest):
        prior = self.command(key, action=action, argument_digest=argument_digest)
        if prior:
            return prior.public_receipt
        if (
            self._conn.execute("SELECT count(*) FROM app_control_commands").fetchone()[
                0
            ]
            >= self._store.limits.command_cap
        ):
            raise CapacityExceeded("control command quota full")
        return None

    def _finish(self, key, argument_digest, receipt):
        self._write_one(
            "INSERT INTO app_control_commands VALUES (?,?,?,?,?,?)",
            (
                key.principal_id,
                key.session_scope,
                key.command_id,
                receipt.action,
                argument_digest,
                _encode(receipt, 16384),
            ),
        )
        retained = self.command(
            key, action=receipt.action, argument_digest=argument_digest
        )
        if retained is None or retained.public_receipt != receipt:
            raise StoreUnavailable("control receipt readback mismatch")
        return receipt

    def _scope(self, key, session_id):
        identifier(session_id)
        if key.session_scope != "control:" + session_id:
            raise CommandConflict("command session scope mismatch")

    def _grant(self, runtime, grant, *, advance):
        # Historical records are decoded without filesystem I/O. Authority
        # admission, including live-session reads, checks every root here.
        for root in grant.roots:
            path = Path(root)
            if str(path.resolve()) != root or not path.is_dir():
                raise CommandConflict("live grant root unavailable or changed")
        row = self._conn.execute(
            "SELECT revision,digest,source_digest FROM app_control_grant_revisions WHERE runtime=? AND grant_id=?",
            (runtime, grant.grant_id),
        ).fetchone()
        hashed = _authority_digest(grant)
        source = hashlib.sha256(
            _encode(
                {
                    "catalog": grant.catalog_digest,
                    "identity": grant.catalog_file_identity,
                }
            ).encode()
        ).hexdigest()
        if row and (
            grant.revision < row[0]
            or grant.revision == row[0]
            and (hashed != row[1] or source != row[2])
        ):
            raise CommandConflict("grant policy rollback or equivocation")
        if advance:
            self._write_one(
                "INSERT INTO app_control_grant_revisions VALUES (?,?,?,?,?) ON CONFLICT(runtime,grant_id) DO UPDATE SET revision=excluded.revision,digest=excluded.digest,source_digest=excluded.source_digest",
                (runtime, grant.grant_id, grant.revision, hashed, source),
            )
            self._grant(runtime, grant, advance=False)
        elif (
            row is None
            or grant.revision != row[0]
            or hashed != row[1]
            or source != row[2]
        ):
            raise CommandConflict("grant revision is no longer current")

    def _quota(self, table, owner, owner_limit, global_limit):
        total = self._conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        count = self._conn.execute(
            f"SELECT count(*) FROM {table} WHERE principal=?", (owner,)
        ).fetchone()[0]
        if total >= global_limit or count >= owner_limit:
            raise CapacityExceeded("control record quota full")

    def read_session(self, *, principal_id, control_session_id):
        self._check()
        principal(principal_id)
        identifier(control_session_id)
        row = self._conn.execute(
            "SELECT principal,runtime,record FROM app_control_sessions WHERE id=?",
            (control_session_id,),
        ).fetchone()
        if row is None or row[0] != principal_id:
            return None
        value = _decode(row[2], ControlSessionRecord)
        if (
            value.principal_id != row[0]
            or value.runtime_id != row[1]
            or value.control_session_id != control_session_id
        ):
            raise StoreUnavailable("session scope corruption")
        return value

    def require_session(self, *, principal_id, control_session_id):
        """Require scoped live session and persisted grant high-water match."""
        return self._live_session(principal_id, control_session_id)

    def _live_session(self, owner, session_id):
        value = self.read_session(principal_id=owner, control_session_id=session_id)
        if value is None:
            raise NotFound("control session unavailable")
        if (
            value.revoked_at is not None
            or not value.issued_at <= self._now() < value.expires_at
        ):
            raise CommandConflict("control session expired or revoked")
        self._grant(value.runtime_id, value.grant, advance=False)
        return value

    def read_binding(self, *, principal_id, binding_id):
        self._check()
        principal(principal_id)
        identifier(binding_id)
        row = self._conn.execute(
            "SELECT principal,runtime,host,record FROM app_host_bindings WHERE id=?",
            (binding_id,),
        ).fetchone()
        if row is None or row[0] != principal_id:
            return None
        value = _decode(row[3], BindingRecord)
        if (
            value.principal_id != row[0]
            or value.runtime_id != row[1]
            or value.canonical_host_id != row[2]
            or value.binding_id != binding_id
        ):
            raise StoreUnavailable("binding scope corruption")
        return value

    def _live_binding(self, session, binding_id):
        value = self.read_binding(
            principal_id=session.principal_id, binding_id=binding_id
        )
        if value is None:
            raise NotFound("binding unavailable")
        if (
            value.runtime_id != session.runtime_id
            or value.grant != session.grant
            or value.revoked_at is not None
            or not value.created_at <= self._now() < value.expires_at
        ):
            raise CommandConflict("binding authority changed")
        self._grant(value.runtime_id, value.grant, advance=False)
        return value

    def read_selection(self, *, principal_id, control_session_id):
        self._check()
        principal(principal_id)
        identifier(control_session_id)
        row = self._conn.execute(
            "SELECT record FROM app_control_selections WHERE principal=? AND session=?",
            (principal_id, control_session_id),
        ).fetchone()
        if row is None:
            return None
        value = _decode(row[0], SelectionRecord)
        if (
            value.principal_id != principal_id
            or value.control_session_id != control_session_id
        ):
            raise StoreUnavailable("selection scope corruption")
        return value

    def commit_enrollment(
        self, key, *, argument_digest, session, replace_session_id=None
    ):
        if (
            type(session) is not ControlSessionRecord
            or session.principal_id != key.principal_id
            or key.session_scope != "account:" + session.account_session_ref
        ):
            raise CommandConflict("enrollment scope mismatch")
        prior = self._start(key, "enroll", argument_digest)
        if prior:
            return prior
        if (
            session.revoked_at is not None
            or session.expires_at
            > session.issued_at + self._store.limits.session_ttl_seconds
            or not session.issued_at <= self._now() < session.expires_at
        ):
            raise CommandConflict("new session is not live")
        if (
            self.read_session(
                principal_id=session.principal_id,
                control_session_id=session.control_session_id,
            )
            is not None
        ):
            raise CommandConflict("session identity already exists")
        old = None
        if replace_session_id is not None:
            old = self.read_session(
                principal_id=session.principal_id, control_session_id=replace_session_id
            )
            if (
                old is None
                or old.account_session_ref != session.account_session_ref
                or old.runtime_id != session.runtime_id
            ):
                raise NotFound("replacement session unavailable")
        limits = self._store.limits
        self._quota(
            "app_control_sessions",
            session.principal_id,
            limits.account_session_cap,
            limits.global_session_cap,
        )
        self._grant(session.runtime_id, session.grant, advance=True)
        if old is not None:
            self._revoke_session(old)
        self._write_one(
            "INSERT INTO app_control_sessions VALUES (?,?,?,?)",
            (
                session.control_session_id,
                session.principal_id,
                session.runtime_id,
                _encode(session),
            ),
        )
        if (
            self.read_session(
                principal_id=session.principal_id,
                control_session_id=session.control_session_id,
            )
            != session
        ):
            raise StoreUnavailable("control session readback mismatch")
        return self._finish(
            key,
            argument_digest,
            CommandReceipt(
                key.command_id, "enroll", "COMMITTED", session.control_session_id
            ),
        )

    def _revoke_session(self, session):
        revoked = replace(session, revoked_at=self._now())
        self._write_one(
            "UPDATE app_control_sessions SET record=? WHERE id=?",
            (
                _encode(revoked),
                session.control_session_id,
            ),
        )
        if (
            self.read_session(
                principal_id=session.principal_id,
                control_session_id=session.control_session_id,
            )
            != revoked
        ):
            raise StoreUnavailable("session revocation readback mismatch")
        selection = self.read_selection(
            principal_id=session.principal_id,
            control_session_id=session.control_session_id,
        )
        if selection:
            self._put_selection(
                replace(
                    selection,
                    epoch=selection.epoch + 1,
                    binding_id=None,
                    binding_revision=None,
                )
            )

    def create_binding(self, key, *, argument_digest, control_session_id, binding):
        self._scope(key, control_session_id)
        prior = self._start(key, "create_binding", argument_digest)
        if prior:
            return prior
        session = self._live_session(key.principal_id, control_session_id)
        if (
            type(binding) is not BindingRecord
            or binding.principal_id != session.principal_id
            or binding.runtime_id != session.runtime_id
            or binding.grant != session.grant
            or binding.expires_at
            > min(
                session.account_expires_at,
                binding.created_at + self._store.limits.binding_ttl_seconds,
            )
            or binding.revision != 1
            or binding.revoked_at is not None
            or not binding.created_at <= self._now() < binding.expires_at
        ):
            raise CommandConflict("new binding exceeds original session")
        if self._conn.execute(
            "SELECT 1 FROM app_host_bindings WHERE id=? OR host=?",
            (binding.binding_id, binding.canonical_host_id),
        ).fetchone():
            raise CommandConflict("binding identity already exists")
        limits = self._store.limits
        self._quota(
            "app_host_bindings",
            key.principal_id,
            limits.account_binding_cap,
            limits.global_binding_cap,
        )
        self._write_one(
            "INSERT INTO app_host_bindings(id,host,principal,runtime,record) VALUES (?,?,?,?,?)",
            (
                binding.binding_id,
                binding.canonical_host_id,
                binding.principal_id,
                binding.runtime_id,
                _encode(binding),
            ),
        )
        if (
            self.read_binding(
                principal_id=binding.principal_id, binding_id=binding.binding_id
            )
            != binding
        ):
            raise StoreUnavailable("binding readback mismatch")
        return self._finish(
            key,
            argument_digest,
            CommandReceipt(
                key.command_id,
                "create_binding",
                "COMMITTED",
                binding.binding_id,
                binding.revision,
            ),
        )

    def _put_selection(self, value):
        self._write_one(
            "INSERT INTO app_control_selections VALUES (?,?,?) ON CONFLICT(principal,session) DO UPDATE SET record=excluded.record",
            (value.principal_id, value.control_session_id, _encode(value)),
        )
        if (
            self.read_selection(
                principal_id=value.principal_id,
                control_session_id=value.control_session_id,
            )
            != value
        ):
            raise StoreUnavailable("selection readback mismatch")

    def select_binding(
        self,
        key,
        *,
        argument_digest,
        control_session_id,
        binding_id,
        expected_binding_revision,
        expected_epoch,
    ):
        self._scope(key, control_session_id)
        prior = self._start(key, "select_binding", argument_digest)
        if prior:
            return prior
        session = self._live_session(key.principal_id, control_session_id)
        binding = self._live_binding(session, binding_id)
        current = self.read_selection(
            principal_id=key.principal_id, control_session_id=control_session_id
        )
        epoch = current.epoch if current else 0
        if (
            type(expected_epoch) is not int
            or expected_epoch != epoch
            or type(expected_binding_revision) is not int
            or expected_binding_revision != binding.revision
        ):
            raise CommandConflict("stale selection or binding revision")
        selection = SelectionRecord(
            key.principal_id,
            control_session_id,
            uuid.uuid4().hex,
            epoch + 1,
            binding_id,
            binding.revision,
        )
        self._put_selection(selection)
        return self._finish(
            key,
            argument_digest,
            CommandReceipt(
                key.command_id,
                "select_binding",
                "COMMITTED",
                selection.selection_id,
                binding.revision,
                selection.epoch,
            ),
        )

    def clear_selection(
        self, key, *, argument_digest, control_session_id, expected_epoch
    ):
        self._scope(key, control_session_id)
        prior = self._start(key, "clear_selection", argument_digest)
        if prior:
            return prior
        self._live_session(key.principal_id, control_session_id)
        current = self.read_selection(
            principal_id=key.principal_id, control_session_id=control_session_id
        )
        if (
            current is None
            or type(expected_epoch) is not int
            or current.epoch != expected_epoch
        ):
            raise CommandConflict("stale selection epoch")
        self._put_selection(
            replace(
                current, epoch=current.epoch + 1, binding_id=None, binding_revision=None
            )
        )
        return self._finish(
            key,
            argument_digest,
            CommandReceipt(
                key.command_id,
                "clear_selection",
                "COMMITTED",
                current.selection_id,
                selection_epoch=current.epoch + 1,
            ),
        )

    def revoke_binding(
        self, key, *, argument_digest, control_session_id, binding_id, expected_revision
    ):
        self._scope(key, control_session_id)
        prior = self._start(key, "revoke_binding", argument_digest)
        if prior:
            return prior
        session = self._live_session(key.principal_id, control_session_id)
        binding = self._live_binding(session, binding_id)
        if type(expected_revision) is not int or expected_revision != binding.revision:
            raise CommandConflict("stale binding revision")
        binding = replace(
            binding, revision=binding.revision + 1, revoked_at=self._now()
        )
        selections = []
        for row in self._conn.execute(
            "SELECT principal,session,record FROM app_control_selections WHERE principal=?",
            (key.principal_id,),
        ).fetchall():
            selection = _decode(row[2], SelectionRecord)
            if (
                selection.principal_id != row[0]
                or row[0] != key.principal_id
                or selection.control_session_id != row[1]
            ):
                raise StoreUnavailable("selection row scope mismatch")
            selections.append(selection)
        self._write_one(
            "UPDATE app_host_bindings SET record=? WHERE id=?",
            (_encode(binding), binding_id),
        )
        if (
            self.read_binding(
                principal_id=binding.principal_id, binding_id=binding.binding_id
            )
            != binding
        ):
            raise StoreUnavailable("binding revocation readback mismatch")
        for selection in selections:
            if selection.binding_id == binding_id:
                self._put_selection(
                    replace(
                        selection,
                        epoch=selection.epoch + 1,
                        binding_id=None,
                        binding_revision=None,
                    )
                )
        return self._finish(
            key,
            argument_digest,
            CommandReceipt(
                key.command_id,
                "revoke_binding",
                "COMMITTED",
                binding_id,
                binding.revision,
            ),
        )

    def require_selection(
        self,
        *,
        principal_id,
        control_session_id,
        binding_id,
        binding_revision,
        selection_id,
        epoch,
    ):
        if type(epoch) is not int or type(binding_revision) is not int:
            raise ValueError("integer selection revision required")
        identifier(selection_id)
        session = self._live_session(principal_id, control_session_id)
        binding = self._live_binding(session, binding_id)
        selection = self.read_selection(
            principal_id=principal_id, control_session_id=control_session_id
        )
        if (
            selection is None
            or selection.binding_id != binding_id
            or selection.binding_revision != binding_revision
            or binding.revision != binding_revision
            or selection.selection_id != selection_id
            or selection.epoch != epoch
        ):
            raise CommandConflict("selection is no longer current")
        return session, binding, selection

    def list_bindings(
        self, *, principal_id, after_position=0, limit=50, max_bytes=65536
    ):
        self._check()
        principal(principal_id)
        positive(limit, self._store.limits.page_cap)
        positive(max_bytes, 65536)
        if type(after_position) is not int or after_position < 0:
            raise ValueError("invalid page cursor")
        rows = self._conn.execute(
            "SELECT position,id,length(record) FROM app_host_bindings WHERE principal=? AND position>? ORDER BY position LIMIT ?",
            (principal_id, after_position, limit + 1),
        ).fetchall()
        values = []
        used = 0
        last = after_position
        for position, bid, _size in rows:
            if len(values) == limit:
                break
            value = self.read_binding(principal_id=principal_id, binding_id=bid)
            size = len(_encode(value).encode("utf8"))
            if used + size > max_bytes:
                break
            values.append(value)
            used += size
            last = position
        if rows and not values:
            raise CapacityExceeded("binding exceeds page byte bound")
        return BindingPage(tuple(values), last if len(values) < len(rows) else None)
