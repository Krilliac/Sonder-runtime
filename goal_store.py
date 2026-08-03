"""Durable conversational goals: one objective that survives turns.

Autopilot already owns *batch* goal execution — a plan, host-verified task
receipts, and a completion gate the model cannot talk its way past. What it
cannot do is keep an objective alive across ordinary conversation turns, which
is where long work actually erodes: the objective gets restated a little
smaller each turn until "make the suite green" has quietly become "fix this
one test".

This module is the missing bookkeeping. It stores an objective plus its
success criteria, re-exposes them to every turn, and records progress notes.
It grants no new authority whatsoever: it starts nothing, executes nothing,
and closes nothing on its own.

Trust boundary (mirrors selfmod.py and autopilot_store.py):

* A goal is created only by an explicit user action.
* A goal is closed only by an explicit user action. Model output can add
  progress notes; it can never mark a goal complete, abandon one, or adopt a
  proposal. ``complete()`` and ``abandon()`` require ``actor="user"`` and
  raise otherwise, so a future caller cannot quietly hand the model the pen.
* Proposals are inert. ``propose()`` records something worth doing; only
  ``adopt()`` — user-only — turns one into an active goal. Nothing in this
  module ever promotes a proposal on its own.

Storage is a per-user SQLite database beside the other Sonder state. Every
read path fail-softs to "no goal" so a corrupt or unwritable store can never
break a conversation.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid

import sonder_paths

MAX_OBJECTIVE_CHARS = 8_000
MAX_CRITERION_CHARS = 500
MAX_CRITERIA = 12
MAX_NOTE_CHARS = 1_000
MAX_NOTES_KEPT = 50
MAX_PROPOSALS = 25

STATUS_ACTIVE = "active"
STATUS_COMPLETED = "completed"
STATUS_ABANDONED = "abandoned"
STATUS_PROPOSED = "proposed"
STATUS_DECLINED = "declined"
OPEN_STATUSES = (STATUS_ACTIVE,)

_LOCAL = threading.local()
_SCHEMA = """
CREATE TABLE IF NOT EXISTS goals (
    id TEXT PRIMARY KEY,
    objective TEXT NOT NULL,
    criteria_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL DEFAULT 'user',
    notes_json TEXT NOT NULL DEFAULT '[]',
    created REAL NOT NULL,
    updated REAL NOT NULL,
    closed REAL,
    closed_by TEXT DEFAULT '',
    close_reason TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS goals_status ON goals(status, updated);
"""


class GoalError(RuntimeError):
    pass


def database_path() -> str:
    return sonder_paths.state_path("goals.db", "SONDER_GOAL_DB")


def _connection():
    path = database_path()
    conn = getattr(_LOCAL, "conn", None)
    if conn is not None and getattr(_LOCAL, "path", None) == path:
        return conn
    if conn is not None:
        with _suppress_sqlite():
            conn.close()
    conn = sqlite3.connect(path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    _LOCAL.conn = conn
    _LOCAL.path = path
    return conn


class _suppress_sqlite:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return exc_type is not None and issubclass(exc_type, sqlite3.Error)


def _clean(value, limit) -> str:
    return " ".join(str(value or "").split())[:limit]


def _criteria(raw) -> list:
    if isinstance(raw, str):
        raw = [part for part in raw.split(";") if part.strip()]
    if not isinstance(raw, (list, tuple)):
        return []
    cleaned = []
    for item in raw:
        text = _clean(item, MAX_CRITERION_CHARS)
        if text and text not in cleaned:
            cleaned.append(text)
        if len(cleaned) >= MAX_CRITERIA:
            break
    return cleaned


def _row(row) -> dict:
    if row is None:
        return {}
    try:
        criteria = json.loads(row["criteria_json"])
        notes = json.loads(row["notes_json"])
    except (TypeError, ValueError):
        criteria, notes = [], []
    return {
        "id": row["id"],
        "objective": row["objective"],
        "criteria": criteria if isinstance(criteria, list) else [],
        "notes": notes if isinstance(notes, list) else [],
        "status": row["status"],
        "scope": row["scope"],
        "origin": row["origin"],
        "created": row["created"],
        "updated": row["updated"],
        "closed": row["closed"],
        "closed_by": row["closed_by"],
        "close_reason": row["close_reason"],
    }


def _require_user(actor, action):
    """Closure and adoption are user-only; this is the whole trust boundary.

    The comparison is deliberately exact rather than case/whitespace-forgiving.
    Being liberal in what an authorization check accepts is how a near-miss
    string ends up authorizing something; the only legitimate callers pass the
    literal "user" from a slash-command path.
    """
    if actor != "user":
        raise GoalError(
            "%s requires an explicit user action (actor=%r); model output "
            "cannot decide a goal's fate" % (action, actor)
        )


def set_goal(objective, criteria=(), scope="", origin="user") -> dict:
    """Create an active goal, superseding any current one in the same scope."""
    text = _clean(objective, MAX_OBJECTIVE_CHARS)
    if not text:
        raise GoalError("a goal needs an objective")
    scope = _clean(scope, 200)
    now = time.time()
    goal_id = "goal-%s" % uuid.uuid4().hex[:12]
    conn = _connection()
    with conn:
        conn.execute(
            "UPDATE goals SET status=?, closed=?, closed_by=?, close_reason=?, "
            "updated=? WHERE status=? AND scope=?",
            (STATUS_ABANDONED, now, "user", "superseded by a new goal", now,
             STATUS_ACTIVE, scope),
        )
        conn.execute(
            "INSERT INTO goals(id, objective, criteria_json, status, scope, "
            "origin, notes_json, created, updated) "
            "VALUES(?, ?, ?, ?, ?, ?, '[]', ?, ?)",
            (goal_id, text, json.dumps(_criteria(criteria)), STATUS_ACTIVE,
             scope, _clean(origin, 40) or "user", now, now),
        )
    return get_active(scope) or {}


def get_active(scope="") -> dict | None:
    """The open goal for a scope, or None. Never raises."""
    try:
        conn = _connection()
        row = conn.execute(
            "SELECT * FROM goals WHERE status=? AND scope=? "
            "ORDER BY updated DESC LIMIT 1",
            (STATUS_ACTIVE, _clean(scope, 200)),
        ).fetchone()
    except sqlite3.Error:
        return None
    return _row(row) or None


def add_note(text, scope="") -> dict | None:
    """Append a bounded progress note to the active goal.

    This is the one write model output may cause, and it is deliberately inert:
    a note records observation, never a verdict.
    """
    note = _clean(text, MAX_NOTE_CHARS)
    if not note:
        return get_active(scope)
    goal = get_active(scope)
    if goal is None:
        return None
    notes = list(goal["notes"])
    notes.append({"at": time.time(), "text": note})
    try:
        conn = _connection()
        with conn:
            conn.execute(
                "UPDATE goals SET notes_json=?, updated=? WHERE id=?",
                (json.dumps(notes[-MAX_NOTES_KEPT:]), time.time(), goal["id"]),
            )
    except sqlite3.Error:
        return goal
    return get_active(scope)


def complete(reason="", scope="", actor="") -> dict:
    """Close the active goal as done. User action only."""
    _require_user(actor, "completing a goal")
    return _close(STATUS_COMPLETED, reason, scope)


def abandon(reason="", scope="", actor="") -> dict:
    """Close the active goal without completing it. User action only."""
    _require_user(actor, "abandoning a goal")
    return _close(STATUS_ABANDONED, reason, scope)


def _close(status, reason, scope) -> dict:
    goal = get_active(scope)
    if goal is None:
        raise GoalError("no active goal in this scope")
    now = time.time()
    conn = _connection()
    with conn:
        conn.execute(
            "UPDATE goals SET status=?, closed=?, closed_by=?, close_reason=?, "
            "updated=? WHERE id=?",
            (status, now, "user", _clean(reason, MAX_NOTE_CHARS), now,
             goal["id"]),
        )
    return {**goal, "status": status, "close_reason": _clean(reason, 200)}


def propose(objective, criteria=(), scope="", source="") -> dict | None:
    """Record something worth doing. Inert until a user adopts it.

    Duplicate objectives in the same scope are ignored so a repeating
    diagnostic cannot flood the queue.
    """
    text = _clean(objective, MAX_OBJECTIVE_CHARS)
    if not text:
        return None
    scope = _clean(scope, 200)
    now = time.time()
    try:
        conn = _connection()
        existing = conn.execute(
            "SELECT 1 FROM goals WHERE status=? AND scope=? AND objective=?",
            (STATUS_PROPOSED, scope, text),
        ).fetchone()
        if existing is not None:
            return None
        goal_id = "prop-%s" % uuid.uuid4().hex[:12]
        with conn:
            conn.execute(
                "INSERT INTO goals(id, objective, criteria_json, status, "
                "scope, origin, notes_json, created, updated) "
                "VALUES(?, ?, ?, ?, ?, ?, '[]', ?, ?)",
                (goal_id, text, json.dumps(_criteria(criteria)),
                 STATUS_PROPOSED, scope, _clean(source, 40) or "self", now,
                 now),
            )
            conn.execute(
                "UPDATE goals SET status=?, updated=? WHERE id IN ("
                "SELECT id FROM goals WHERE status=? AND scope=? "
                "ORDER BY created DESC LIMIT -1 OFFSET ?)",
                (STATUS_DECLINED, now, STATUS_PROPOSED, scope, MAX_PROPOSALS),
            )
    except sqlite3.Error:
        return None
    return get(goal_id)


def proposals(scope="", limit=20) -> list:
    try:
        conn = _connection()
        rows = conn.execute(
            "SELECT * FROM goals WHERE status=? AND scope=? "
            "ORDER BY created DESC LIMIT ?",
            (STATUS_PROPOSED, _clean(scope, 200), max(1, int(limit))),
        ).fetchall()
    except (sqlite3.Error, TypeError, ValueError):
        return []
    return [_row(row) for row in rows]


def adopt(goal_id, scope="", actor="") -> dict:
    """Promote a proposal to the active goal. User action only."""
    _require_user(actor, "adopting a proposal")
    proposal = get(goal_id)
    if not proposal or proposal["status"] != STATUS_PROPOSED:
        raise GoalError("no proposal %r to adopt" % goal_id)
    now = time.time()
    conn = _connection()
    with conn:
        conn.execute("DELETE FROM goals WHERE id=?", (proposal["id"],))
    goal = set_goal(
        proposal["objective"], proposal["criteria"],
        scope=scope or proposal["scope"], origin="adopted",
    )
    conn = _connection()
    with conn:
        conn.execute("UPDATE goals SET updated=? WHERE id=?",
                     (now, goal["id"]))
    return goal


def decline(goal_id, actor="") -> dict:
    """Dismiss a proposal without adopting it. User action only."""
    _require_user(actor, "declining a proposal")
    proposal = get(goal_id)
    if not proposal or proposal["status"] != STATUS_PROPOSED:
        raise GoalError("no proposal %r to decline" % goal_id)
    conn = _connection()
    with conn:
        conn.execute(
            "UPDATE goals SET status=?, updated=? WHERE id=?",
            (STATUS_DECLINED, time.time(), proposal["id"]),
        )
    return {**proposal, "status": STATUS_DECLINED}


def get(goal_id) -> dict | None:
    try:
        conn = _connection()
        row = conn.execute(
            "SELECT * FROM goals WHERE id=?", (str(goal_id or ""),),
        ).fetchone()
    except sqlite3.Error:
        return None
    return _row(row) or None


def history(limit=10, scope="") -> list:
    try:
        conn = _connection()
        rows = conn.execute(
            "SELECT * FROM goals WHERE status IN (?, ?) AND scope=? "
            "ORDER BY updated DESC LIMIT ?",
            (STATUS_COMPLETED, STATUS_ABANDONED, _clean(scope, 200),
             max(1, int(limit))),
        ).fetchall()
    except (sqlite3.Error, TypeError, ValueError):
        return []
    return [_row(row) for row in rows]


def context_block(scope="") -> str:
    """The active goal rendered for injection into a turn's system context.

    The anti-drift wording is the entire point: without it a long objective
    gets quietly restated smaller each turn until the easy part is mistaken
    for the whole job.
    """
    goal = get_active(scope)
    if goal is None:
        return ""
    lines = [
        "ACTIVE GOAL (persists across turns; the user set it, only the user "
        "closes it)",
        "  objective: %s" % goal["objective"],
    ]
    for criterion in goal["criteria"]:
        lines.append("  success criterion: %s" % criterion)
    for note in goal["notes"][-5:]:
        lines.append("  progress note: %s" % note["text"])
    lines.extend([
        "  Keep the full objective intact. If it cannot be finished in this "
        "turn, make concrete progress toward the real end state and say what "
        "remains — do not redefine success as the part that fits.",
        "  You may record progress notes. You cannot mark this goal complete; "
        "report the evidence and let the user decide.",
    ])
    return "\n".join(lines)


def reset_for_tests():
    conn = getattr(_LOCAL, "conn", None)
    if conn is not None:
        with _suppress_sqlite():
            conn.close()
    _LOCAL.conn = None
    _LOCAL.path = None
