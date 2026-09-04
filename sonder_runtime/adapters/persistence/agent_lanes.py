"""Interactive lane metadata over the retained fleet mailbox.

Commands, lane bindings, instructions and transcript outbox entries share one
SQLite transaction. Canonical session projection uses stable event identities;
projection failure never loses an accepted command and is retried before work.
"""

from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
import secrets
import threading
import time
import uuid
from pathlib import Path
from .fleet_store import _ensure_schema as _ensure_fleet_schema

_SCHEMA = """
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
"""


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class LaneTransaction:
    def __init__(self, conn):
        self.conn = conn

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
        conn = sqlite3.connect(self.path, timeout=5)
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

    def open_parent(self, principal):
        session = "model-parent-" + uuid.uuid4().hex
        salt, token = secrets.token_hex(16), secrets.token_urlsafe(32)
        expires = time.time() + 3600
        with self.transaction() as tx:
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

    def acquire_owner(self, owner):
        from .lane_owner import LocalLaneOwner

        return LocalLaneOwner(self.path, owner)

    def reconcile(self, lane_id, principal):
        from .lane_owner import owner_definitely_stopped

        with self.transaction() as tx:
            lane = tx.lane(lane_id)
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
