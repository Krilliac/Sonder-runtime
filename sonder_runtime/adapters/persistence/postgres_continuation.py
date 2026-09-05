"""PostgreSQL implementation of the existing durable child aggregate."""

from dataclasses import asdict, replace
import json
import re
import uuid
import time
from threading import Lock

from ...application.ports.continuation_mutations import (
    PreparedContinuationMutation,
    ContinuationMutationOutcome,
    ContinuationStorageFailure,
    ContinuationCommitAmbiguous,
    ContinuationReceiptCapacity,
    prepare_call,
    canonical,
)
from ...application.ports.continuation_records import DurableChildSession
from ...application.ports.subagents import (
    InvalidSubagentRequest,
    SubagentStatus,
    TERMINAL_SUBAGENT_STATUSES,
)
from ...application.subagents.continuation_codec import decode_call, session_from_data
from .postgres_continuation_transport import PostgresContinuationTransport

_SCHEMA = """
CREATE SCHEMA IF NOT EXISTS sonder_child;
CREATE TABLE IF NOT EXISTS sonder_child.meta(id integer PRIMARY KEY CHECK(id=1),version integer NOT NULL,barrier bigint NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS sonder_child.owner(id integer PRIMARY KEY CHECK(id=1),owner_id text NOT NULL,incarnation text NOT NULL,clean boolean NOT NULL);
CREATE TABLE IF NOT EXISTS sonder_child.child_lock(child_id text PRIMARY KEY);
CREATE TABLE IF NOT EXISTS sonder_child.child(position bigserial UNIQUE NOT NULL,child_id text PRIMARY KEY,status text NOT NULL,revision bigint NOT NULL,snapshot bytea NOT NULL);
CREATE TABLE IF NOT EXISTS sonder_child.intent(position bigserial UNIQUE NOT NULL,operation_id text PRIMARY KEY,child_id text NOT NULL,kind text NOT NULL,digest text NOT NULL,payload bytea NOT NULL);
CREATE INDEX IF NOT EXISTS intent_child_position ON sonder_child.intent(child_id,position);
CREATE TABLE IF NOT EXISTS sonder_child.receipt(operation_id text PRIMARY KEY REFERENCES sonder_child.intent(operation_id),disposition text NOT NULL,result bytea NOT NULL,revision bigint);
"""
_OWNER_KEY = (1397706308, 1128810828)  # Fixed aggregate identity, never caller input.


def _prepared(row):
    return (
        None
        if row is None
        else PreparedContinuationMutation(row[0], row[1], row[2], bytes(row[3]), row[4])
    )


class PostgreSQLDurableContinuationRepository:
    def __init__(self, config, binding):
        self.config, self.binding = config, binding
        self._transport = PostgresContinuationTransport(config, binding)
        self._incarnation = uuid.uuid4().hex
        self._owner_connection = None
        self._owner_fenced = False
        self._admitted = False
        self._closed = False
        self._admissions_stopped = False
        self._close_lock = Lock()
        try:
            self._transport.run(self._schema)
            self._owner_connection = self._transport.connection_class.connect(
                **binding.connection_kwargs(config)
            )
            self._transport.run(self._claim_owner, connection=self._owner_connection)
            self._admitted = True
        except Exception:
            self._owner_fenced = True
            if self._transport.quiescent() and self._owner_connection is not None:
                self._owner_connection.close()
            self._transport.close(timeout=config.cancel_timeout_seconds)
            raise

    def _policy(self, connection):
        row = connection.execute(
            "SELECT pg_is_in_recovery(),current_setting('synchronous_standby_names'),current_setting('server_version_num')::integer"
        ).fetchone()
        if row[0] or not 180006 <= row[2] < 190000:
            raise ContinuationStorageFailure(
                "configured PostgreSQL primary/version unavailable"
            )
        if self.config.durability == "sync-pair":
            match = re.fullmatch(
                r'FIRST\s+1\s*\(\s*(?:"([A-Za-z0-9_-]{1,64})"|([A-Za-z0-9_-]{1,64}))\s*\)',
                row[1],
            )
            if match is None or (match[1] or match[2]) != self.config.required_standby:
                raise ContinuationStorageFailure(
                    "configured synchronous standby policy unavailable"
                )
        connection.execute(
            "SELECT set_config('synchronous_commit',%s,true)",
            ("remote_apply" if self.config.durability == "sync-pair" else "on",),
        )

    def _begin(self, connection):
        self._transport.require_effect_boundary()
        connection.execute("BEGIN")
        self._policy(connection)

    def _schema(self, connection):
        self._begin(connection)
        existed = connection.execute(
            "SELECT to_regclass('sonder_child.child'),to_regclass('sonder_child.meta')"
        ).fetchone()
        if existed[0] is not None and existed[1] is None:
            raise ContinuationStorageFailure("child storage schema metadata is missing")
        connection.execute(_SCHEMA)
        row = connection.execute(
            "SELECT version FROM sonder_child.meta WHERE id=1"
        ).fetchone()
        if row is None:
            if existed[0] is not None:
                raise ContinuationStorageFailure(
                    "child storage schema version is missing"
                )
            connection.execute("INSERT INTO sonder_child.meta(id,version) VALUES(1,1)")
        elif row[0] != 1:
            raise ContinuationStorageFailure("unsupported child storage schema version")
        connection.commit()

    def _claim_owner(self, connection):
        # Acquire the aggregate-constant session lock before the durable claim.
        if not connection.execute(
            "SELECT pg_try_advisory_lock(%s,%s)", _OWNER_KEY
        ).fetchone()[0]:
            raise ContinuationStorageFailure("child execution owner already active")
        self._owner_identity = connection.execute(
            "SELECT pid,backend_start FROM pg_stat_activity WHERE pid=pg_backend_pid()"
        ).fetchone()
        connection.commit()
        self._begin(connection)
        row = connection.execute(
            "SELECT owner_id,incarnation,clean FROM sonder_child.owner WHERE id=1 FOR UPDATE"
        ).fetchone()
        if row is None:
            existing = connection.execute(
                "SELECT (SELECT count(*) FROM sonder_child.child)+(SELECT count(*) FROM sonder_child.intent)+(SELECT count(*) FROM sonder_child.receipt)"
            ).fetchone()[0]
            if existing:
                raise ContinuationStorageFailure(
                    "child execution owner metadata is missing"
                )
            connection.execute(
                "INSERT INTO sonder_child.owner VALUES(1,%s,%s,false)",
                (self.config.owner_id, self._incarnation),
            )
        else:
            if row[0] != self.config.owner_id or not row[2]:
                raise ContinuationStorageFailure(
                    "child owner cleanup or configured identity is unproven"
                )
            connection.execute(
                "UPDATE sonder_child.owner SET incarnation=%s,clean=false WHERE id=1",
                (self._incarnation,),
            )
        connection.commit()

    def _require_owner(self):
        if (
            not self._admitted
            or self._closed
            or self._owner_fenced
            or self._admissions_stopped
        ):
            raise ContinuationStorageFailure("child execution owner is unavailable")
        if self._owner_connection.closed:
            self._owner_fenced = True
            raise ContinuationStorageFailure("child execution owner session was lost")

    def _owner_row(self, connection):
        held = connection.execute(
            "SELECT EXISTS(SELECT 1 FROM pg_locks l JOIN pg_stat_activity a ON a.pid=l.pid WHERE l.locktype=%s AND l.pid=%s AND a.backend_start=%s AND l.classid=%s AND l.objid=%s AND l.granted)",
            (
                "advisory",
                self._owner_identity[0],
                self._owner_identity[1],
                _OWNER_KEY[0],
                _OWNER_KEY[1],
            ),
        ).fetchone()[0]
        if not held:
            self._owner_fenced = True
            raise ContinuationStorageFailure("child execution owner session was lost")
        row = connection.execute(
            "SELECT owner_id,incarnation,clean FROM sonder_child.owner WHERE id=1"
        ).fetchone()
        if row != (self.config.owner_id, self._incarnation, False):
            self._owner_fenced = True
            raise ContinuationStorageFailure(
                "child execution owner incarnation changed"
            )

    @staticmethod
    def _lock_child(connection, child_id):
        connection.execute(
            "INSERT INTO sonder_child.child_lock VALUES(%s) ON CONFLICT DO NOTHING",
            (child_id,),
        )
        connection.execute(
            "SELECT child_id FROM sonder_child.child_lock WHERE child_id=%s FOR UPDATE",
            (child_id,),
        ).fetchone()

    @staticmethod
    def _get(connection, child_id):
        row = connection.execute(
            "SELECT snapshot FROM sonder_child.child WHERE child_id=%s", (child_id,)
        ).fetchone()
        return session_from_data(json.loads(bytes(row[0]))) if row else None

    @staticmethod
    def _retained(connection, prepared):
        row = connection.execute(
            "SELECT digest FROM sonder_child.intent WHERE operation_id=%s",
            (prepared.operation_id,),
        ).fetchone()
        if row and row[0] != prepared.request_sha256:
            raise InvalidSubagentRequest(
                "operation identity already has different input"
            )
        return row is not None

    def _receipt(self, connection, prepared):
        self._retained(connection, prepared)
        row = connection.execute(
            "SELECT disposition,result,revision FROM sonder_child.receipt WHERE operation_id=%s",
            (prepared.operation_id,),
        ).fetchone()
        return (
            ContinuationMutationOutcome(row[0], bytes(row[1]), row[2], True)
            if row
            else None
        )

    @staticmethod
    def _capacity(connection, extra=0):
        connection.execute(
            "SELECT id FROM sonder_child.meta WHERE id=1 FOR UPDATE"
        ).fetchone()
        count, size = connection.execute(
            "SELECT count(*),coalesce(sum(octet_length(payload)),0) FROM sonder_child.intent"
        ).fetchone()
        receipts = connection.execute(
            "SELECT coalesce(sum(octet_length(result)),0) FROM sonder_child.receipt"
        ).fetchone()[0]
        if count > 100000 or size + receipts + extra > 64 * 1024 * 1024:
            raise ContinuationReceiptCapacity("continuation receipt capacity exhausted")

    def mutate(self, prepared):
        if not isinstance(prepared, PreparedContinuationMutation):
            raise TypeError("prepared mutation required")
        if "\x00" in prepared.child_id or "\x00" in prepared.operation_id:
            raise InvalidSubagentRequest("PostgreSQL identifiers cannot contain NUL")
        args, kwargs = decode_call(prepared)
        if (
            prepare_call(
                prepared.kind, *args, operation_id=prepared.operation_id, **kwargs
            )
            != prepared
        ):
            raise InvalidSubagentRequest(
                "prepared mutation arguments do not match identity"
            )
        self._require_owner()

        def prove_committed_owner(connection, outcome):
            # The dedicated session can disappear while this leased connection
            # commits. An existing receipt alone cannot publish owner success.
            self._begin(connection)
            self._owner_row(connection)
            connection.rollback()
            return outcome

        def apply(connection):
            self._begin(connection)
            self._owner_row(connection)
            self._lock_child(connection, prepared.child_id)
            self._capacity(connection)
            if not self._retained(connection, prepared):
                connection.execute(
                    "INSERT INTO sonder_child.intent(operation_id,child_id,kind,digest,payload) VALUES(%s,%s,%s,%s,%s)",
                    (
                        prepared.operation_id,
                        prepared.child_id,
                        prepared.kind,
                        prepared.request_sha256,
                        prepared.payload,
                    ),
                )
                self._capacity(connection)
            connection.commit()
            self._begin(connection)
            self._owner_row(connection)
            self._lock_child(connection, prepared.child_id)
            prior = self._receipt(connection, prepared)
            if prior is not None:
                if self.config.durability == "sync-pair":
                    connection.execute(
                        "UPDATE sonder_child.meta SET barrier=barrier+1 WHERE id=1"
                    )
                connection.commit()
                return prove_committed_owner(connection, prior)
            pending = connection.execute(
                "SELECT i.kind,i.child_id,i.operation_id,i.payload,i.digest FROM sonder_child.intent i LEFT JOIN sonder_child.receipt r USING(operation_id) WHERE i.child_id=%s AND r.operation_id IS NULL ORDER BY i.position LIMIT 1",
                (prepared.child_id,),
            ).fetchone()
            if pending is not None and pending[2] != prepared.operation_id:
                raise ContinuationCommitAmbiguous(_prepared(pending))
            current = self._get(connection, prepared.child_id)
            try:
                next_record, value = _apply(prepared.kind, current, args, kwargs)
            except InvalidSubagentRequest as error:
                next_record, value = None, None
                outcome = ContinuationMutationOutcome(
                    "invalid", canonical({"error": str(error)}), None
                )
            else:
                disposition = (
                    "precondition_failed"
                    if value is None
                    else "no_change" if value is False else "applied"
                )
                outcome = ContinuationMutationOutcome(
                    disposition,
                    canonical(
                        asdict(value)
                        if isinstance(value, DurableChildSession)
                        else value
                    ),
                    value.revision if isinstance(value, DurableChildSession) else None,
                )
            self._capacity(connection, len(outcome.result_bytes))
            if next_record is not None:
                snapshot = canonical(asdict(next_record))
                if current is None:
                    connection.execute(
                        "INSERT INTO sonder_child.child(child_id,status,revision,snapshot) VALUES(%s,%s,%s,%s)",
                        (
                            prepared.child_id,
                            next_record.status.value,
                            next_record.revision,
                            snapshot,
                        ),
                    )
                else:
                    changed = connection.execute(
                        "UPDATE sonder_child.child SET status=%s,revision=%s,snapshot=%s WHERE child_id=%s AND revision=%s",
                        (
                            next_record.status.value,
                            next_record.revision,
                            snapshot,
                            prepared.child_id,
                            current.revision,
                        ),
                    )
                    if changed.rowcount != 1:
                        raise ContinuationStorageFailure(
                            "child revision changed under storage lock"
                        )
            connection.execute(
                "INSERT INTO sonder_child.receipt VALUES(%s,%s,%s,%s)",
                (
                    prepared.operation_id,
                    outcome.disposition,
                    outcome.result_bytes,
                    outcome.resulting_revision,
                ),
            )
            connection.commit()
            return prove_committed_owner(connection, outcome)

        outcome = self._transport.run(apply, prepared=prepared)
        return (
            replace(outcome, storage_acknowledgement="pair_committed")
            if self.config.durability == "sync-pair"
            else outcome
        )

    def _read(self, function):
        self._require_owner()

        def read(connection):
            self._begin(connection)
            self._owner_row(connection)
            result = function(connection)
            connection.rollback()
            return result

        return self._transport.run(read)

    def get(self, child_id):
        return self._read(lambda connection: self._get(connection, child_id))

    def reconcile(self, prepared):
        self._transport.require_reconcilable(prepared)
        return self._read(lambda connection: self._receipt(connection, prepared))

    def read_mutation(self, operation_id):
        return self._read(
            lambda connection: _prepared(
                connection.execute(
                    "SELECT kind,child_id,operation_id,payload,digest FROM sonder_child.intent WHERE operation_id=%s",
                    (operation_id,),
                ).fetchone()
            )
        )

    def latest_mutation(self, child_id):
        return self._read(
            lambda connection: _prepared(
                connection.execute(
                    "SELECT kind,child_id,operation_id,payload,digest FROM sonder_child.intent WHERE child_id=%s ORDER BY position DESC LIMIT 1",
                    (child_id,),
                ).fetchone()
            )
        )

    def unresolved_mutation(self, child_id):
        return self._read(
            lambda connection: _prepared(
                connection.execute(
                    "SELECT i.kind,i.child_id,i.operation_id,i.payload,i.digest FROM sonder_child.intent i LEFT JOIN sonder_child.receipt r USING(operation_id) WHERE i.child_id=%s AND r.operation_id IS NULL ORDER BY i.position LIMIT 1",
                    (child_id,),
                ).fetchone()
            )
        )

    def mutation_ids(self, child_id, *, after=0, limit=100):
        if (
            type(after) is not int
            or after < 0
            or type(limit) is not int
            or not 1 <= limit <= 100
        ):
            raise ValueError("invalid mutation page bounds")
        rows = self._read(
            lambda connection: connection.execute(
                "SELECT position,operation_id FROM sonder_child.intent WHERE child_id=%s AND position>%s ORDER BY position LIMIT %s",
                (child_id, after, limit + 1),
            ).fetchall()
        )
        return tuple(rows[:limit]), len(rows) > limit

    def list_active(self):
        return self._read(
            lambda connection: tuple(
                session_from_data(json.loads(bytes(row[0])))
                for row in connection.execute(
                    "SELECT snapshot FROM sonder_child.child WHERE status NOT IN ('succeeded','failed','timed_out','cancelled') ORDER BY child_id"
                ).fetchall()
            )
        )

    def list_all(self, *, limit=1000):
        if type(limit) is not int or not 1 <= limit <= 2**31 - 1:
            raise InvalidSubagentRequest("limit must be a positive bounded integer")
        return self._read(
            lambda connection: tuple(
                session_from_data(json.loads(bytes(row[0])))
                for row in connection.execute(
                    "SELECT snapshot FROM sonder_child.child ORDER BY position LIMIT %s",
                    (limit,),
                ).fetchall()
            )
        )

    def create(self, session):
        return self.mutate(prepare_call("create", session)).value

    def save_checkpoint(self, checkpoint, *, expected_sequence):
        return self.mutate(
            prepare_call(
                "save_checkpoint", checkpoint, expected_sequence=expected_sequence
            )
        ).value

    def update(
        self,
        child_id,
        *,
        status,
        expected_revision=None,
        usage=None,
        result=None,
        recovery_required=None,
    ):
        return self.mutate(
            prepare_call(
                "update",
                child_id,
                status=status,
                expected_revision=expected_revision,
                usage=usage,
                result=result,
                recovery_required=recovery_required,
            )
        ).value

    def claim_resume(self, child_id, *, expected_revision):
        return self.mutate(
            prepare_call("claim_resume", child_id, expected_revision=expected_revision)
        ).value

    def request_cancel(self, child_id, *, reason):
        return self.mutate(
            prepare_call("request_cancel", child_id, reason=reason)
        ).value

    def close(self, *, runners_stopped=False, timeout=5):
        if not self._close_lock.acquire(blocking=False):
            return False
        try:
            return self._close_owned(runners_stopped=runners_stopped, timeout=timeout)
        finally:
            self._close_lock.release()

    def _close_owned(self, *, runners_stopped, timeout):
        deadline = time.monotonic() + max(0, timeout)
        if self._closed:
            return True
        self.stop_admissions()
        if not runners_stopped or not self._transport.quiescent():
            return False
        # Stop/join pool-owned workers before publishing clean owner eligibility.
        # The dedicated nonpooled owner session remains locked for the marker.
        if not self._transport.close(timeout=max(0, deadline - time.monotonic())):
            return False
        if self._owner_fenced:
            self._owner_connection.close()
            self._closed = self._owner_connection.closed
            if self._closed:
                self.binding.close()
            return (
                self._closed
            )  # Durable unclean marker remains; this is not takeover eligibility.

        def clean(connection):
            self._begin(connection)
            self._owner_row(connection)
            held = connection.execute(
                "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype=%s AND pid=pg_backend_pid() AND classid=%s AND objid=%s AND granted)",
                ("advisory", _OWNER_KEY[0], _OWNER_KEY[1]),
            ).fetchone()[0]
            if not held:
                self._owner_fenced = True
                raise ContinuationStorageFailure(
                    "child execution owner session was lost"
                )
            connection.execute(
                "UPDATE sonder_child.owner SET clean=true WHERE id=1 AND incarnation=%s",
                (self._incarnation,),
            )
            connection.commit()

        self._transport.run(
            clean,
            connection=self._owner_connection,
            shutdown=True,
            timeout=max(0, deadline - time.monotonic()),
        )
        self._owner_connection.close()
        self._closed = self._owner_connection.closed and self._transport.close(
            timeout=max(0, deadline - time.monotonic())
        )
        if self._closed:
            self.binding.close()
        return self._closed

    def stop_admissions(self):
        self._admissions_stopped = True
        self._transport.stop_admissions()


def _apply(kind, current, args, kwargs):
    if kind == "create":
        if current is not None:
            raise InvalidSubagentRequest("child_id already exists")
        return args[0], args[0]
    if kind == "request_cancel":
        if not kwargs["reason"].strip():
            raise InvalidSubagentRequest("cancellation reason is required")
        if current is None:
            raise InvalidSubagentRequest(f"unknown child_id {args[0]!r}")
        if (
            current.cancellation_requested
            or current.status in TERMINAL_SUBAGENT_STATUSES
        ):
            return None, False
        return (
            replace(
                current,
                cancellation_requested=True,
                cancellation_reason=kwargs["reason"],
                revision=current.revision + 1,
            ),
            True,
        )
    if current is None:
        return None, None
    if kind == "save_checkpoint":
        expected = current.checkpoint.sequence if current.checkpoint else -1
        if expected != kwargs["expected_sequence"] or args[0].sequence != expected + 1:
            return None, None
        result = replace(current, checkpoint=args[0], revision=current.revision + 1)
    elif kind == "claim_resume":
        if (
            current.revision != kwargs["expected_revision"]
            or current.status not in (SubagentStatus.FAILED, SubagentStatus.TIMED_OUT)
            or not current.recovery_required
            or current.cancellation_requested
        ):
            return None, None
        result = replace(
            current,
            status=SubagentStatus.RUNNING,
            revision=current.revision + 1,
            result=None,
            recovery_required=False,
        )
    else:
        if (
            kwargs.get("expected_revision") is not None
            and current.revision != kwargs["expected_revision"]
        ) or (
            current.status in TERMINAL_SUBAGENT_STATUSES
            and kwargs["status"] not in TERMINAL_SUBAGENT_STATUSES
        ):
            return None, None
        values = {
            key: value
            for key, value in kwargs.items()
            if key != "expected_revision" and value is not None
        }
        result = replace(current, **values, revision=current.revision + 1)
    return result, result
