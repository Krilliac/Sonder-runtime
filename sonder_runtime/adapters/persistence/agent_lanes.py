"""Interactive lane metadata over the retained fleet mailbox.

Commands, lane bindings, instructions and transcript outbox entries share one
SQLite transaction. Canonical session projection uses stable event identities;
projection failure never loses an accepted command and is retried before work.
"""

from __future__ import annotations

from sonder_runtime.adapters.persistence.owned_sqlite import connect as owned_sqlite_connect
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import sqlite3
import secrets
import threading
import time
import uuid
from pathlib import Path
from .fleet_store import _ensure_schema as _ensure_fleet_schema

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_lane_terminal_results (
 continuation_id TEXT NOT NULL, verification_id TEXT NOT NULL, principal TEXT NOT NULL,
 original_digest TEXT NOT NULL, binding TEXT NOT NULL, payload BLOB NOT NULL,
 digest TEXT NOT NULL, certificate_digest TEXT NOT NULL, receipt TEXT NOT NULL,
 PRIMARY KEY(continuation_id,verification_id));
CREATE TABLE IF NOT EXISTS agent_lane_continuations (
 position INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
 principal TEXT NOT NULL, parent_session TEXT UNIQUE NOT NULL,
 host_conversation TEXT NOT NULL, data TEXT NOT NULL,
 UNIQUE(principal,host_conversation));
CREATE TABLE IF NOT EXISTS agent_lane_terminal_projections (
 continuation_id TEXT NOT NULL, principal TEXT NOT NULL,
 verification_id TEXT NOT NULL, binding TEXT NOT NULL,
 payload BLOB NOT NULL, digest TEXT NOT NULL,
 PRIMARY KEY(continuation_id,verification_id));
CREATE TABLE IF NOT EXISTS agent_parent_verification (
 parent_session TEXT PRIMARY KEY, principal TEXT NOT NULL,
 generation INTEGER NOT NULL DEFAULT 0, barrier TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS agent_verifications (
 id TEXT PRIMARY KEY, parent_session TEXT NOT NULL, principal TEXT NOT NULL,
 command_id TEXT NOT NULL, data TEXT NOT NULL, UNIQUE(principal,command_id));

CREATE TABLE IF NOT EXISTS agent_lanes (
 position INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
 principal TEXT NOT NULL, parent_session TEXT NOT NULL, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS agent_lanes_owner ON agent_lanes(principal,position);
CREATE TABLE IF NOT EXISTS agent_lane_roots (
 session_id TEXT PRIMARY KEY, principal TEXT NOT NULL, agent_id TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS agent_lane_parent_grants (
 session_id TEXT PRIMARY KEY, principal TEXT NOT NULL, salt TEXT NOT NULL,
 digest TEXT NOT NULL, expires REAL NOT NULL, revision INTEGER NOT NULL,
 revoked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS agent_lane_commands (
 principal TEXT NOT NULL, id TEXT NOT NULL, digest TEXT NOT NULL, receipt TEXT NOT NULL,
 PRIMARY KEY(principal,id));
CREATE TABLE IF NOT EXISTS agent_lane_events (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, lane_id TEXT NOT NULL,
 session_id TEXT NOT NULL, event_id TEXT UNIQUE NOT NULL, event_type TEXT NOT NULL,
 payload TEXT NOT NULL, occurred_at TEXT NOT NULL, projected INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS agent_lane_events_lane ON agent_lane_events(lane_id,sequence);
CREATE TABLE IF NOT EXISTS agent_lane_messages (
 message_id TEXT PRIMARY KEY REFERENCES fleet_messages(message_id), lane_id TEXT NOT NULL,
 sequence INTEGER NOT NULL, author TEXT NOT NULL, delivery_state TEXT NOT NULL,
 attempt_id TEXT NOT NULL DEFAULT '', report INTEGER NOT NULL DEFAULT 0,
 acknowledged INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS agent_lane_messages_lane ON agent_lane_messages(lane_id,sequence);
CREATE INDEX IF NOT EXISTS agent_lane_reports_sequence ON agent_lane_messages(report,sequence);
"""


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class LaneTransaction:
    def __init__(self, conn):
        self.conn = conn

    def link_terminal_projection(self, continuation_id, principal, sealed):
        """Private persistence primitive; caller must authorize the bound link.

        No upsert: a retry can only confirm the identical original host record.
        The enclosing continuation admission transaction owns all authority checks.
        """
        from dataclasses import asdict

        sealed.validate()
        if (
            sealed.binding.continuation_id != continuation_id
            or sealed.binding.principal_id != principal
        ):
            raise PermissionError("sealed projection scope mismatch")
        for value in (continuation_id, principal):
            if not isinstance(value, str) or not 1 <= len(value.encode()) <= 256:
                raise ValueError("projection scope is invalid")
        binding = encode(asdict(sealed.binding))
        row = self.conn.execute(
            "SELECT principal,binding,payload,digest FROM agent_lane_terminal_projections "
            "WHERE continuation_id=? AND verification_id=?",
            (continuation_id, sealed.binding.verification_id),
        ).fetchone()
        expected = (principal, binding, sealed.payload, sealed.sha256)
        if row is not None:
            if tuple(row) != expected:
                raise ValueError("original terminal projection is immutable")
            return
        self.conn.execute(
            "INSERT INTO agent_lane_terminal_projections VALUES (?,?,?,?,?,?)",
            (
                continuation_id,
                principal,
                sealed.binding.verification_id,
                binding,
                sealed.payload,
                sealed.sha256,
            ),
        )

    def terminal_projection(self, continuation_id, principal, verification_id):
        from ...application.ports.lane_continuation import (
            ProjectionBinding,
            SealedProjection,
        )

        row = self.conn.execute(
            "SELECT binding,payload,digest FROM agent_lane_terminal_projections "
            "WHERE continuation_id=? AND principal=? AND verification_id=?",
            (continuation_id, principal, verification_id),
        ).fetchone()
        if row is None:
            raise KeyError("terminal projection unavailable")
        binding = json.loads(row["binding"])
        binding["project_roots"] = tuple(binding["project_roots"])
        sealed = SealedProjection(
            ProjectionBinding(**binding), row["payload"], row["digest"]
        )
        sealed.validate()
        if (
            sealed.binding.continuation_id != continuation_id
            or sealed.binding.principal_id != principal
            or sealed.binding.verification_id != verification_id
        ):
            raise PermissionError("stored projection scope mismatch")
        return sealed

    def verification_generation(self, parent, principal):
        row = self.conn.execute(
            "SELECT principal,generation FROM agent_parent_verification WHERE parent_session=?",
            (parent,),
        ).fetchone()
        if row is None:
            self.root(parent, principal)
            self.conn.execute(
                "INSERT INTO agent_parent_verification VALUES (?,?,0,'')",
                (parent, principal),
            )
            return 0
        if row[0] != principal:
            raise PermissionError("verification parent belongs to another principal")
        return row[1]

    def bump_verification(self, lane):
        parent, principal = lane["parent_session_id"], lane["principal_id"]
        self.verification_generation(parent, principal)
        self.conn.execute(
            "UPDATE agent_parent_verification SET generation=generation+1 WHERE parent_session=?",
            (parent,),
        )

    def verification_barrier(self, parent, principal):
        self.verification_generation(parent, principal)
        return self.conn.execute(
            "SELECT barrier FROM agent_parent_verification WHERE parent_session=?",
            (parent,),
        ).fetchone()[0]

    def verification_dispatch_blocked(self, lane):
        if self.verification_barrier(lane["parent_session_id"], lane["principal_id"]):
            return True
        rows = self.conn.execute(
            "SELECT v.data FROM agent_verifications v JOIN agent_parent_verification p ON p.barrier=v.id LIMIT 10001"
        ).fetchall()
        if len(rows) > 10000:
            raise ValueError("global verification barrier bound exceeded")
        root = Path(lane["workspace_root"]).resolve()
        for row in rows:
            for raw in json.loads(row[0])["prepared"]["roots"]:
                other = Path(raw).resolve()
                if root == other or root in other.parents or other in root.parents:
                    return True
        return False

    def require_verification_workspace_quiescence(self, parent, principal, roots):
        """Check global ownership in the same writer transaction as admission."""
        requested = tuple(Path(root).resolve() for root in roots)

        def overlaps(raw):
            other = Path(raw).resolve()
            return any(
                root == other or root in other.parents or other in root.parents
                for root in requested
            )

        barriers = self.conn.execute(
            "SELECT v.data FROM agent_parent_verification p LEFT JOIN agent_verifications v ON v.id=p.barrier WHERE p.barrier<>'' LIMIT 10001"
        ).fetchall()
        if len(barriers) > 10000:
            raise ValueError("global verification barrier bound exceeded")
        for row in barriers:
            if row[0] is None or any(
                overlaps(root) for root in json.loads(row[0])["prepared"]["roots"]
            ):
                raise ValueError("workspace overlaps an existing verification barrier")
        rows = self.conn.execute(
            """SELECT l.id, json_extract(l.data,'$.workspace_root') AS root,
                json_extract(l.data,'$.status') AS status,
                json_extract(l.data,'$.owner') AS owner,
                json_extract(l.data,'$.pending_effect') AS pending_effect,
                json_extract(l.data,'$.pending_response') AS pending_response,
                EXISTS(SELECT 1 FROM agent_lane_messages m WHERE m.lane_id=l.id AND m.report=0 AND m.delivery_state='queued') AS queued
                FROM agent_lanes l WHERE NOT (l.parent_session=? AND l.principal=?)
                ORDER BY l.position LIMIT 10001""",
            (parent, principal),
        ).fetchall()
        if len(rows) > 10000:
            raise ValueError("global lane ownership bound exceeded")
        for row in rows:
            if overlaps(row["root"]) and (
                row["status"] not in {"completed", "failed", "cancelled", "interrupted"}
                or row["owner"]
                or row["pending_effect"]
                or row["pending_response"]
                or row["queued"]
            ):
                raise ValueError("workspace overlaps nonquiescent foreign lane")

    def acquire_verification_barrier(self, parent, principal, verification_id):
        current = self.verification_barrier(parent, principal)
        if current and current != verification_id:
            raise ValueError("verification barrier already owned")
        self.conn.execute(
            "UPDATE agent_parent_verification SET barrier=? WHERE parent_session=?",
            (verification_id, parent),
        )

    def release_verification_barrier(self, parent, principal, verification_id):
        if self.verification_barrier(parent, principal) != verification_id:
            raise ValueError("verification barrier ownership changed")
        self.conn.execute(
            "UPDATE agent_parent_verification SET barrier='' WHERE parent_session=?",
            (parent,),
        )

    def verification_children(self, parent, principal):
        self.verification_generation(parent, principal)
        children = self.lanes(principal, parent, limit=257)
        if len(children) > 256:
            raise ValueError("verification child bound exceeded")
        return [lane for _, lane in children]

    def verification_row(self, verification_id, principal):
        row = self.conn.execute(
            "SELECT principal,data FROM agent_verifications WHERE id=?",
            (verification_id,),
        ).fetchone()
        if row is None:
            raise KeyError("verification not found")
        if row[0] != principal:
            raise PermissionError("verification belongs to another principal")
        return json.loads(row[1])

    def save_verification(self, value):
        self.conn.execute(
            "UPDATE agent_verifications SET data=? WHERE id=? AND principal=?",
            (encode(value), value["verification_id"], value["principal_id"]),
        )

    def lane(self, lane_id):
        row = self.conn.execute(
            "SELECT data FROM agent_lanes WHERE id=?", (lane_id,)
        ).fetchone()
        if row is None:
            raise KeyError("agent lane not found")
        return json.loads(row[0])

    def lanes(self, principal, parent_session=None, cursor=0, limit=100):
        sql = "SELECT position,data FROM agent_lanes WHERE principal=? AND position>?"
        args = [principal, cursor]
        if parent_session is not None:
            sql += " AND parent_session=?"
            args.append(parent_session)
        rows = self.conn.execute(
            sql + " ORDER BY position LIMIT ?", (*args, limit)
        ).fetchall()
        return [(r[0], json.loads(r[1])) for r in rows]

    def all_lanes(self):
        rows = self.conn.execute(
            "SELECT data FROM agent_lanes ORDER BY position LIMIT 10001"
        ).fetchall()
        if len(rows) > 10000:
            raise ValueError("global workspace reservation capacity reached")
        return [json.loads(r[0]) for r in rows]

    def root(self, session, principal):
        row = self.conn.execute(
            "SELECT principal,agent_id FROM agent_lane_roots WHERE session_id=?",
            (session,),
        ).fetchone()
        if row:
            if row[0] != principal:
                raise PermissionError("parent session belongs to another principal")
            return row[1]
        agent_id = (
            "lane-root-"
            + hashlib.sha256((principal + "\0" + session).encode()).hexdigest()[:32]
        )
        self.conn.execute(
            "INSERT INTO agent_lane_roots VALUES (?,?,?)",
            (session, principal, agent_id),
        )
        self.agent(agent_id, principal, "", "parent", "running")
        return agent_id

    def agent(self, agent_id, principal, parent_id, task, status):
        now = time.time()
        self.conn.execute(
            """INSERT INTO fleet_agents
          (id,owner_id,owner_pid,principal_id,role,parent_id,task,status,started_ts,updated_ts)
          VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                agent_id,
                "interactive-lanes",
                0,
                principal,
                "agent_lane",
                parent_id,
                "Interactive agent lane (scoped transcript)",
                status,
                now,
                now,
            ),
        )

    def insert(self, lane):
        self.bump_verification(lane)
        self.agent(
            lane["id"],
            lane["principal_id"],
            lane["mailbox_parent"],
            lane["task"],
            "queued",
        )
        self.conn.execute(
            "INSERT INTO agent_lanes(id,principal,parent_session,data) VALUES (?,?,?,?)",
            (lane["id"], lane["principal_id"], lane["parent_session_id"], encode(lane)),
        )

    def save(self, lane):
        self.bump_verification(lane)
        lane["revision"] += 1
        self.conn.execute(
            "UPDATE agent_lanes SET data=? WHERE id=?", (encode(lane), lane["id"])
        )
        status = {
            "completed": "completed",
            "interrupt_requested": "running",
            "cancel_requested": "running",
            "awaiting_input": "interrupted",
        }.get(lane["status"], lane["status"])
        # Full prompts/results stay in canonical session/mailbox, fleet metadata is bounded.
        self.conn.execute(
            "UPDATE fleet_agents SET status=?,updated_ts=?,activity=? WHERE id=?",
            (status, time.time(), lane["status"], lane["id"]),
        )

    def receipt(self, principal, command_id, digest):
        row = self.conn.execute(
            "SELECT digest,receipt FROM agent_lane_commands WHERE principal=? AND id=?",
            (principal, command_id),
        ).fetchone()
        if row:
            if row[0] != digest:
                raise ValueError("command_id already used with different input")
            return json.loads(row[1])
        return None

    def record_receipt(self, principal, command_id, digest, receipt):
        self.conn.execute(
            "INSERT INTO agent_lane_commands VALUES (?,?,?,?)",
            (principal, command_id, digest, encode(receipt)),
        )

    def emit(self, lane, event_type, payload, session_id=None):
        if len(encode(payload).encode("utf-8")) > 131072:
            raise ValueError("lane event exceeds durable payload bound")
        event_id = "lane-event-" + uuid.uuid4().hex
        cursor = self.conn.execute(
            """INSERT INTO agent_lane_events
          (lane_id,session_id,event_id,event_type,payload,occurred_at) VALUES (?,?,?,?,?,?)""",
            (
                lane["id"],
                session_id or lane["session_id"],
                event_id,
                event_type,
                encode(payload),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        return cursor.lastrowid

    def message(self, lane, content, author, *, report=False, source_sequence=0):
        pending = self.conn.execute(
            """SELECT COUNT(*) FROM agent_lane_messages
            WHERE lane_id=? AND delivery_state='queued' AND report=0""",
            (lane["id"],),
        ).fetchone()[0]
        if not report and pending >= 32:
            raise ValueError("lane pending message limit reached")
        message_id = "msg-" + uuid.uuid4().hex
        sender, recipient = (
            (lane["id"], lane["mailbox_parent"])
            if report
            else (lane["mailbox_parent"], lane["id"])
        )
        now = time.time()
        self.conn.execute(
            """INSERT INTO fleet_messages
          (message_id,sender_id,recipient_id,owner_id,principal_id,project_scope,scope_root_id,
           mode,body,status,queued_ts,expires_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                message_id,
                sender,
                recipient,
                "interactive-lanes",
                lane["principal_id"],
                lane["workspace_root"],
                lane["root_id"],
                "follow_up",
                content,
                "queued",
                now,
                now + 86400 * 30,
            ),
        )
        payload = {
            "message_id": message_id,
            "id": message_id,
            "author": author,
            "content": content,
            "attempt_id": lane["attempt_id"],
            "source_sequence": source_sequence,
            "artifacts": list(lane.get("artifacts", [])) if report else [],
        }
        sequence = self.emit(
            lane,
            "lane.report" if report else "lane.message",
            payload,
            session_id=lane["parent_session_id"] if report else None,
        )
        self.conn.execute(
            "INSERT INTO agent_lane_messages VALUES (?,?,?,?,?,?,?,0)",
            (
                message_id,
                lane["id"],
                sequence,
                author,
                "queued",
                lane["attempt_id"] if report else "",
                int(report),
            ),
        )
        return message_id

    def unread_report_count(self, lane_id):
        """Count all unacknowledged reports without retrieving mailbox bodies."""
        return self.conn.execute(
            "SELECT COUNT(*) FROM agent_lane_messages "
            "WHERE lane_id=? AND report=1 AND acknowledged=0",
            (lane_id,),
        ).fetchone()[0]

    def messages(self, lane_id, *, report=False, limit=100):
        rows = self.conn.execute(
            """SELECT m.*,f.body,e.payload AS event_payload FROM agent_lane_messages m
          JOIN fleet_messages f ON f.message_id=m.message_id JOIN agent_lane_events e ON e.sequence=m.sequence WHERE m.lane_id=? AND m.report=?
          ORDER BY m.sequence DESC LIMIT ?""",
            (lane_id, int(report), limit),
        ).fetchall()
        return [
            dict(
                id=r["message_id"],
                sequence=r["sequence"],
                author=r["author"],
                content=r["body"],
                delivery_state=r["delivery_state"],
                attempt_id=r["attempt_id"],
                acknowledged=bool(r["acknowledged"]),
                source_sequence=json.loads(r["event_payload"]).get(
                    "source_sequence", 0
                ),
                artifacts=json.loads(r["event_payload"]).get("artifacts", []),
            )
            for r in reversed(rows)
        ]

    def report_page(self, principal, parent_session, cursor, limit):
        """Seek metadata first, then load bodies only for the bounded selected page."""
        page_sql = (
            "SELECT m.message_id,m.lane_id,m.attempt_id,m.sequence,m.acknowledged "
            "FROM agent_lane_messages m JOIN agent_lanes l ON l.id=m.lane_id "
            "WHERE l.principal=? AND m.report=1 AND m.sequence>?"
        )
        parameters = [principal, cursor]
        if parent_session is not None:
            page_sql += " AND l.parent_session=?"
            parameters.append(parent_session)
        page_sql += " ORDER BY m.sequence LIMIT ?"
        parameters.append(limit)
        rows = self.conn.execute(
            "SELECT page.*,f.body,e.payload AS event_payload FROM ("
            + page_sql
            + ") page "
            "JOIN fleet_messages f ON f.message_id=page.message_id "
            "JOIN agent_lane_events e ON e.sequence=page.sequence ORDER BY page.sequence",
            parameters,
        ).fetchall()
        result = []
        for row in rows:
            event = json.loads(row["event_payload"])
            result.append(
                dict(
                    id=row["message_id"],
                    lane_id=row["lane_id"],
                    attempt_id=row["attempt_id"],
                    source_sequence=event.get("source_sequence", 0),
                    sequence=row["sequence"],
                    summary=row["body"],
                    artifacts=list(event.get("artifacts", [])),
                    acknowledged=bool(row["acknowledged"]),
                )
            )
        return result

    def accepted(self, lane, ids):
        for message_id in ids:
            self.conn.execute(
                "UPDATE agent_lane_messages SET delivery_state='accepted',attempt_id=? WHERE message_id=? AND delivery_state='queued'",
                (lane["attempt_id"], message_id),
            )
            self.conn.execute(
                "UPDATE fleet_messages SET status='delivered',delivered_ts=? WHERE message_id=?",
                (time.time(), message_id),
            )
        self.emit(
            lane,
            "lane.messages.accepted",
            {"message_ids": ids, "attempt_id": lane["attempt_id"]},
        )

    def handled(self, lane, ids):
        for message_id in ids:
            self.conn.execute(
                "UPDATE agent_lane_messages SET delivery_state='handled' WHERE message_id=?",
                (message_id,),
            )
        self.emit(
            lane,
            "lane.messages.handled",
            {"message_ids": ids, "attempt_id": lane["attempt_id"]},
        )

    def acknowledge(self, report_id, principal, parent_session_id=None):
        row = self.conn.execute(
            "SELECT lane_id FROM agent_lane_messages WHERE message_id=? AND report=1",
            (report_id,),
        ).fetchone()
        if not row:
            raise KeyError("report not found")
        lane = self.lane(row[0])
        if lane["principal_id"] != principal or (
            parent_session_id is not None
            and lane["parent_session_id"] != parent_session_id
        ):
            raise PermissionError("report belongs to another principal or parent inbox")
        self.conn.execute(
            "UPDATE agent_lane_messages SET acknowledged=1,delivery_state='handled' WHERE message_id=?",
            (report_id,),
        )
        self.conn.execute(
            "UPDATE fleet_messages SET status='delivered',delivered_ts=? WHERE message_id=?",
            (time.time(), report_id),
        )
        self.emit(
            lane,
            "lane.report.acknowledged",
            {"report_id": report_id},
            lane["parent_session_id"],
        )
        self.save(lane)
        return lane


class SQLiteAgentLaneStore:
    def __init__(self, path, sessions):
        self.path = str(Path(path).resolve())
        self.sessions = sessions
        self._projection_lock = threading.RLock()
        _ensure_fleet_schema(self.path)
        with self.connect() as conn:
            conn.executescript(_SCHEMA)

    def connect(self):
        conn = owned_sqlite_connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @contextmanager
    def transaction(self):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield LaneTransaction(conn)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def flush(self):
        """Replay outbox exactly once into canonical history, before execution.

        A crash after append but before projected=1 is recognized by event ID;
        a mismatched existing event fails closed instead of hiding corruption.
        """
        with self._projection_lock:
            while True:
                with self.connect() as conn:
                    row = conn.execute(
                        "SELECT * FROM agent_lane_events WHERE projected=0 ORDER BY sequence LIMIT 1"
                    ).fetchone()
                if row is None:
                    return
                payload = json.loads(row["payload"])
                self.sessions.append_once(
                    row["session_id"],
                    row["event_type"],
                    payload,
                    event_id=row["event_id"],
                    occurred_at_utc=row["occurred_at"],
                )
                with self.connect() as conn:
                    conn.execute(
                        "UPDATE agent_lane_events SET projected=1 WHERE sequence=?",
                        (row["sequence"],),
                    )

    @staticmethod
    def _proof(salt, token):
        return hashlib.sha256((salt + "\0" + token).encode()).hexdigest()

    def open_parent(self, principal, *, transaction=None, expires_at=None):
        if transaction is None:
            with self.transaction() as tx:
                return self.open_parent(
                    principal, transaction=tx, expires_at=expires_at
                )
        if (
            not isinstance(transaction, LaneTransaction)
            or not transaction.conn.in_transaction
        ):
            raise PermissionError("active parent creation transaction required")
        if expires_at is not None and (
            type(expires_at) not in (int, float)
            or not math.isfinite(expires_at)
            or expires_at <= time.time()
        ):
            raise PermissionError("finite live parent expiry required")
        session = "model-parent-" + uuid.uuid4().hex
        salt, token = secrets.token_hex(16), secrets.token_urlsafe(32)
        expires = (
            min(time.time() + 3600, expires_at)
            if expires_at is not None
            else time.time() + 3600
        )
        tx = transaction
        count = tx.conn.execute(
            "SELECT COUNT(*) FROM agent_lane_parent_grants WHERE principal=? AND revoked=0 AND expires>?",
            (principal, time.time()),
        ).fetchone()[0]
        if count >= 32:
            raise ValueError("active parent capability capacity reached")
        tx.root(session, principal)
        tx.conn.execute(
            "INSERT INTO agent_lane_parent_grants VALUES (?,?,?,?,?,1,0)",
            (session, principal, salt, self._proof(salt, token), expires),
        )
        return dict(
            parent_session_id=session,
            parent_token=token,
            revision=1,
            expires_at=expires,
        )

    def _verify_parent(self, tx, session, token, principal):
        if not isinstance(token, str) or not 32 <= len(token) <= 128:
            raise PermissionError("parent capability is invalid")
        row = tx.conn.execute(
            "SELECT * FROM agent_lane_parent_grants WHERE session_id=?", (session,)
        ).fetchone()
        if (
            row is None
            or row["principal"] != principal
            or row["revoked"]
            or row["expires"] <= time.time()
            or not secrets.compare_digest(
                row["digest"], self._proof(row["salt"], token)
            )
        ):
            raise PermissionError("parent capability is invalid or expired")
        return row

    def parent_capability(self, session, token, principal, action="verify"):
        with self.transaction() as tx:
            if tx.conn.execute(
                "SELECT 1 FROM agent_lane_continuations WHERE parent_session=?",
                (session,),
            ).fetchone():
                raise PermissionError(
                    "host-managed root requires a current bound attachment"
                )
            row = self._verify_parent(tx, session, token, principal)
            if action == "verify":
                return None
            revision = row["revision"] + 1
            if action == "revoke":
                tx.conn.execute(
                    "UPDATE agent_lane_parent_grants SET revoked=1,revision=? WHERE session_id=?",
                    (revision, session),
                )
                return dict(parent_session_id=session, revision=revision)
            if action != "rotate":
                raise ValueError("unknown capability action")
            salt, replacement = secrets.token_hex(16), secrets.token_urlsafe(32)
            tx.conn.execute(
                "UPDATE agent_lane_parent_grants SET salt=?,digest=?,revision=? WHERE session_id=?",
                (salt, self._proof(salt, replacement), revision, session),
            )
            return dict(
                parent_session_id=session,
                parent_token=replacement,
                revision=revision,
                expires_at=row["expires"],
            )

    def validate_parent_grant(self, session, principal):
        with self.connect() as conn:
            row = conn.execute(
                "SELECT principal,expires,revoked FROM agent_lane_parent_grants WHERE session_id=?",
                (session,),
            ).fetchone()
        if row and (
            row["principal"] != principal
            or row["revoked"]
            or row["expires"] <= time.time()
        ):
            raise PermissionError("parent capability grant was revoked or expired")

    def read_lane(self, lane_id):
        with self.connect() as conn:
            row = conn.execute(
                "SELECT data FROM agent_lanes WHERE id=?", (lane_id,)
            ).fetchone()
        if row is None:
            raise KeyError("agent lane not found")
        return json.loads(row[0])

    def owner_definitely_stopped(self, owner):
        from .lane_owner import owner_definitely_stopped

        return owner_definitely_stopped(self.path, owner)

    def acquire_owner(self, owner):
        from .lane_owner import LocalLaneOwner

        return LocalLaneOwner(self.path, owner)

    def reconcile(self, lane_id, principal, *, _admit=None):
        from .lane_owner import owner_definitely_stopped

        with self.transaction() as tx:
            lane = tx.lane(lane_id)
            if _admit is not None:
                _admit(tx, lane)
            if lane["principal_id"] != principal:
                raise PermissionError("agent lane belongs to another principal")
            if lane["owner"] and owner_definitely_stopped(self.path, lane["owner"]):
                lane["status"] = (
                    "awaiting_input" if lane["pending_effect"] else "interrupted"
                )
                lane["owner"] = ""
                lane["error"] = (
                    "Local worker exited; admitted effects require reconciliation"
                    if lane["pending_effect"]
                    else "Local worker exited at a safe boundary"
                )
                tx.emit(
                    lane,
                    "lane.owner.exited",
                    {"status": lane["status"], "attempt_id": lane["attempt_id"]},
                )
                tx.save(lane)

    def events(self, lane_id, cursor, limit):
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM agent_lane_events WHERE lane_id=? AND sequence>?
              ORDER BY sequence LIMIT ?""",
                (lane_id, cursor, limit + 1),
            ).fetchall()
        page = []
        size = 0
        for row in rows[:limit]:
            event = dict(
                sequence=row["sequence"],
                event_id=row["event_id"],
                event_type=row["event_type"],
                payload=json.loads(row["payload"]),
            )
            event_size = len(encode(event).encode("utf-8"))
            if page and size + event_size > 262144:
                break
            page.append(event)
            size += event_size
        return page, len(rows) > len(page)
