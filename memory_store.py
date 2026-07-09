"""SQLite-backed memory for the trilobite learning loop. Stdlib only."""
import os
import re
import sqlite3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS interactions (
    id TEXT PRIMARY KEY,
    task TEXT,
    retrieved_ctx TEXT,
    response TEXT,
    tier TEXT,
    ts TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS outcomes (
    interaction_id TEXT,
    signal TEXT,
    reward REAL,
    ts TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS lessons (
    id TEXT PRIMARY KEY,
    text TEXT,
    embedding BLOB,
    source_interaction TEXT,
    ts TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE VIRTUAL TABLE IF NOT EXISTS lessons_fts USING fts5(lesson_id UNINDEXED, text);
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    title TEXT,
    summary TEXT,
    summarized_through TEXT,
    project TEXT,
    created_ts TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_ts TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS facts (
    id TEXT PRIMARY KEY,
    project TEXT,
    text TEXT,
    embedding BLOB,
    ts TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS lesson_usage (
    lesson_id TEXT,
    interaction_id TEXT,
    task TEXT,
    outcome_signal TEXT,
    reward REAL,
    ts TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(lesson_id, interaction_id)
);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    detail TEXT,
    status TEXT DEFAULT 'pending',
    priority INTEGER DEFAULT 2,
    project TEXT,
    owner TEXT,
    parent_id TEXT,
    created_ts TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_ts TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS task_events (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    event TEXT NOT NULL,
    note TEXT,
    ts TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


def connect(path=":memory:", check_same_thread=True):
    conn = sqlite3.connect(path, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    init_db(conn)
    return conn


def _column_names(conn, table):
    return {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table).fetchall()}


def _migrate(conn):
    """Idempotently add columns to pre-existing DBs (fresh DBs get them here too).

    New nullable columns default to NULL on old rows, which every session/recall
    query treats as 'not part of a session / no embedding' — so today's single-turn,
    session-less behavior is preserved for existing data.
    """
    cols = _column_names(conn, "interactions")
    if "session_id" not in cols:
        conn.execute("ALTER TABLE interactions ADD COLUMN session_id TEXT")
    if "task_embedding" not in cols:
        conn.execute("ALTER TABLE interactions ADD COLUMN task_embedding BLOB")


def init_db(conn):
    conn.executescript(_SCHEMA)
    _migrate(conn)
    # Indexes reference migrated columns, so they must come after _migrate.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_interactions_session "
        "ON interactions(session_id, ts)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_facts_project ON facts(project)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_lesson_usage_lesson "
        "ON lesson_usage(lesson_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_lesson_usage_interaction "
        "ON lesson_usage(interaction_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_status_project "
        "ON tasks(status, project, updated_ts)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_events_task "
        "ON task_events(task_id, ts)"
    )
    conn.commit()


def new_id():
    return os.urandom(8).hex()


def log_interaction(conn, interaction_id, task, retrieved_ctx, response, tier,
                    session_id=None, task_embedding=None):
    conn.execute(
        "INSERT INTO interactions"
        "(id, task, retrieved_ctx, response, tier, session_id, task_embedding) "
        "VALUES(?, ?, ?, ?, ?, ?, ?)",
        (interaction_id, task, retrieved_ctx, response, tier, session_id, task_embedding),
    )
    conn.commit()


def get_interaction(conn, interaction_id):
    row = conn.execute(
        "SELECT * FROM interactions WHERE id=?", (interaction_id,)
    ).fetchone()
    return dict(row) if row else None


def record_outcome_row(conn, interaction_id, signal, reward):
    conn.execute(
        "INSERT INTO outcomes(interaction_id, signal, reward) VALUES(?, ?, ?)",
        (interaction_id, signal, reward),
    )
    conn.commit()


def add_lesson(conn, lesson_id, text, embedding, source_interaction):
    conn.execute(
        "INSERT INTO lessons(id, text, embedding, source_interaction) VALUES(?, ?, ?, ?)",
        (lesson_id, text, embedding, source_interaction),
    )
    conn.execute(
        "INSERT INTO lessons_fts(lesson_id, text) VALUES(?, ?)", (lesson_id, text)
    )
    conn.commit()


def lesson_exists_for_interaction(conn, interaction_id):
    row = conn.execute(
        "SELECT 1 FROM lessons WHERE source_interaction=? LIMIT 1",
        (interaction_id,),
    ).fetchone()
    return row is not None


def all_lessons(conn):
    rows = conn.execute("SELECT id, text, embedding FROM lessons").fetchall()
    return [dict(r) for r in rows]


def get_lesson_text(conn, lesson_id):
    row = conn.execute("SELECT text FROM lessons WHERE id=?", (lesson_id,)).fetchone()
    return row[0] if row else None


def delete_lesson(conn, lesson_id):
    """Remove a lesson from both the lessons table and its manual FTS mirror.

    Returns True if a row was deleted. lessons_fts is a plain (non-content) fts5
    table with no delete triggers, so its row must be removed explicitly.
    """
    cur = conn.execute("DELETE FROM lessons WHERE id=?", (lesson_id,))
    conn.execute("DELETE FROM lessons_fts WHERE lesson_id=?", (lesson_id,))
    conn.execute("DELETE FROM lesson_usage WHERE lesson_id=?", (lesson_id,))
    conn.commit()
    return cur.rowcount > 0


def log_lesson_usage(conn, lesson_ids, interaction_id, task):
    for lesson_id in lesson_ids or []:
        conn.execute(
            "INSERT OR IGNORE INTO lesson_usage(lesson_id, interaction_id, task) "
            "VALUES(?, ?, ?)",
            (lesson_id, interaction_id, task),
        )
    conn.commit()


def record_lesson_usage_outcome(conn, interaction_id, signal, reward):
    conn.execute(
        "UPDATE lesson_usage SET outcome_signal=?, reward=? WHERE interaction_id=?",
        (signal, reward, interaction_id),
    )
    conn.commit()


def lesson_usage_stats(conn):
    rows = conn.execute(
        "SELECT lesson_id, COUNT(*) AS uses, "
        "SUM(CASE WHEN reward > 0 THEN 1 ELSE 0 END) AS wins, "
        "SUM(CASE WHEN reward < 0 THEN 1 ELSE 0 END) AS losses, "
        "AVG(CASE WHEN reward IS NOT NULL THEN reward END) AS avg_reward "
        "FROM lesson_usage GROUP BY lesson_id"
    ).fetchall()
    return {r["lesson_id"]: dict(r) for r in rows}


def _sanitize_fts(query):
    # FTS5 MATCH chokes on raw punctuation; reduce to quoted word tokens OR'd together.
    toks = [t for t in re.findall(r"\w+", query.lower()) if len(t) > 2][:32]
    return " OR ".join('"%s"' % t for t in toks)


def fts_search(conn, query, limit=10):
    q = _sanitize_fts(query)
    if not q:
        return []
    rows = conn.execute(
        "SELECT lesson_id FROM lessons_fts WHERE lessons_fts MATCH ? "
        "ORDER BY rank LIMIT ?",
        (q, limit),
    ).fetchall()
    return [r[0] for r in rows]


def count_interactions(conn):
    return conn.execute("SELECT COUNT(*) FROM interactions").fetchone()[0]


def outcome_signal_counts(conn):
    rows = conn.execute("SELECT signal, COUNT(*) FROM outcomes GROUP BY signal").fetchall()
    return {r[0]: r[1] for r in rows}


def recent_lessons(conn, limit=5):
    rows = conn.execute(
        "SELECT id, text, ts FROM lessons ORDER BY ts DESC, rowid DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def interactions_with_good_outcome(conn, good_signals):
    if not good_signals:
        return []
    placeholders = ",".join("?" * len(good_signals))
    rows = conn.execute(
        "SELECT DISTINCT i.id, i.task, i.response FROM interactions i "
        "JOIN outcomes o ON o.interaction_id = i.id "
        "WHERE o.signal IN (%s)" % placeholders,
        tuple(good_signals),
    ).fetchall()
    return [dict(r) for r in rows]


# --- conversation sessions -------------------------------------------------

def session_turns(conn, session_id):
    """All turns for a session, oldest-first, as {id, task, response} dicts.

    ts has only second resolution, so rowid is the tiebreaker for same-second turns.
    """
    rows = conn.execute(
        "SELECT id, task, response FROM interactions WHERE session_id=? "
        "ORDER BY ts ASC, rowid ASC",
        (session_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def session_history(conn, session_id, max_turns=12):
    """Last `max_turns` (task, response) pairs for a session, oldest-first."""
    pairs = [(t["task"], t["response"]) for t in session_turns(conn, session_id)]
    return pairs[-max_turns:] if max_turns and max_turns > 0 else pairs


def session_turn_count(conn, session_id):
    return conn.execute(
        "SELECT COUNT(*) FROM interactions WHERE session_id=?", (session_id,)
    ).fetchone()[0]


# --- visible task/todo state ----------------------------------------------

TASK_STATUSES = {"pending", "in_progress", "blocked", "done", "canceled"}


def _normalize_task_status(status):
    s = (status or "pending").strip().lower().replace("-", "_")
    if s in ("todo", "open"):
        s = "pending"
    if s in ("doing", "active"):
        s = "in_progress"
    if s in ("complete", "completed"):
        s = "done"
    if s not in TASK_STATUSES:
        raise ValueError("unknown task status '%s'" % status)
    return s


def _normalize_priority(priority):
    try:
        value = int(priority)
    except (TypeError, ValueError):
        value = 2
    return max(0, min(5, value))


def log_task_event(conn, task_id, event, note=""):
    conn.execute(
        "INSERT INTO task_events(id, task_id, event, note) VALUES(?, ?, ?, ?)",
        (new_id(), task_id, event, note or ""),
    )
    conn.commit()


def create_task(conn, title, detail="", status="pending", priority=2,
                project="", owner="", parent_id="", task_id=None):
    title = (title or "").strip()
    if not title:
        raise ValueError("task title is required")
    task_id = task_id or new_id()
    normalized = _normalize_task_status(status)
    conn.execute(
        "INSERT INTO tasks(id, title, detail, status, priority, project, owner, parent_id) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
        (
            task_id,
            title,
            detail or "",
            normalized,
            _normalize_priority(priority),
            project or "",
            owner or "",
            parent_id or "",
        ),
    )
    conn.commit()
    log_task_event(conn, task_id, "created", title)
    return get_task(conn, task_id)


def resolve_task_id(conn, task_id):
    value = (task_id or "").strip()
    if not value:
        return None
    row = conn.execute("SELECT id FROM tasks WHERE id=?", (value,)).fetchone()
    if row:
        return row["id"]
    rows = conn.execute(
        "SELECT id FROM tasks WHERE id LIKE ? ORDER BY updated_ts DESC, rowid DESC LIMIT 2",
        (value + "%",),
    ).fetchall()
    if len(rows) == 1:
        return rows[0]["id"]
    return None


def get_task(conn, task_id):
    resolved = resolve_task_id(conn, task_id) or task_id
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (resolved,)).fetchone()
    return dict(row) if row else None


def update_task(conn, task_id, status=None, title=None, detail=None,
                priority=None, project=None, owner=None, note=""):
    resolved = resolve_task_id(conn, task_id)
    if not resolved:
        raise ValueError("no unique task '%s'" % task_id)
    fields = []
    values = []
    event_bits = []
    if status is not None and str(status).strip():
        normalized = _normalize_task_status(status)
        fields.append("status=?")
        values.append(normalized)
        event_bits.append("status=%s" % normalized)
    if title is not None and str(title).strip():
        fields.append("title=?")
        values.append(str(title).strip())
        event_bits.append("title")
    if detail is not None:
        fields.append("detail=?")
        values.append(detail or "")
        event_bits.append("detail")
    if priority is not None and str(priority).strip():
        p = _normalize_priority(priority)
        fields.append("priority=?")
        values.append(p)
        event_bits.append("priority=%s" % p)
    if project is not None:
        fields.append("project=?")
        values.append(project or "")
        event_bits.append("project")
    if owner is not None:
        fields.append("owner=?")
        values.append(owner or "")
        event_bits.append("owner")
    if not fields:
        return get_task(conn, resolved)
    fields.append("updated_ts=CURRENT_TIMESTAMP")
    values.append(resolved)
    conn.execute("UPDATE tasks SET %s WHERE id=?" % ", ".join(fields), tuple(values))
    conn.commit()
    log_task_event(conn, resolved, "updated", note or ", ".join(event_bits))
    return get_task(conn, resolved)


def list_tasks(conn, status="", project="", owner="", limit=50, include_done=False):
    limit = max(1, min(int(limit or 50), 200))
    clauses = []
    values = []
    if status:
        clauses.append("status=?")
        values.append(_normalize_task_status(status))
    elif not include_done:
        clauses.append("status NOT IN ('done', 'canceled')")
    if project:
        clauses.append("project=?")
        values.append(project)
    if owner:
        clauses.append("owner=?")
        values.append(owner)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        "SELECT * FROM tasks%s ORDER BY priority ASC, updated_ts DESC, rowid DESC LIMIT ?"
        % where,
        tuple(values + [limit]),
    ).fetchall()
    return [dict(r) for r in rows]


def task_events(conn, task_id, limit=20):
    resolved = resolve_task_id(conn, task_id)
    if not resolved:
        return []
    rows = conn.execute(
        "SELECT * FROM task_events WHERE task_id=? ORDER BY ts DESC, rowid DESC LIMIT ?",
        (resolved, max(1, min(int(limit or 20), 100))),
    ).fetchall()
    return [dict(r) for r in rows]


def touch_session(conn, session_id, project=None):
    """Ensure a sessions row exists and bump its updated_ts. Preserves title/summary."""
    conn.execute(
        "INSERT INTO sessions(session_id, project) VALUES(?, ?) "
        "ON CONFLICT(session_id) DO UPDATE SET updated_ts=CURRENT_TIMESTAMP",
        (session_id, project),
    )
    # Set project only if it wasn't already set (don't clobber an explicit one).
    if project is not None:
        conn.execute(
            "UPDATE sessions SET project=? WHERE session_id=? AND "
            "(project IS NULL OR project='')",
            (project, session_id),
        )
    conn.commit()


def get_session(conn, session_id):
    row = conn.execute(
        "SELECT * FROM sessions WHERE session_id=?", (session_id,)
    ).fetchone()
    return dict(row) if row else None


def set_session_title(conn, session_id, title):
    conn.execute(
        "UPDATE sessions SET title=? WHERE session_id=?", (title, session_id)
    )
    conn.commit()


def set_session_project(conn, session_id, project):
    conn.execute(
        "UPDATE sessions SET project=? WHERE session_id=?", (project, session_id)
    )
    conn.commit()


def update_session_summary(conn, session_id, summary, summarized_through):
    conn.execute(
        "UPDATE sessions SET summary=?, summarized_through=? WHERE session_id=?",
        (summary, summarized_through, session_id),
    )
    conn.commit()


def list_sessions(conn, limit=20):
    """Sessions most-recently-updated first, with live turn counts."""
    rows = conn.execute(
        "SELECT s.session_id, s.title, s.updated_ts, s.project, "
        "  (SELECT COUNT(*) FROM interactions i WHERE i.session_id=s.session_id) "
        "  AS turn_count "
        "FROM sessions s ORDER BY s.updated_ts DESC, s.rowid DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def find_session(conn, prefix):
    """Resolve a session by exact id, then by a case-insensitive title prefix."""
    row = conn.execute(
        "SELECT session_id FROM sessions WHERE session_id=?", (prefix,)
    ).fetchone()
    if row:
        return row[0]
    row = conn.execute(
        "SELECT session_id FROM sessions WHERE lower(title) LIKE lower(?) "
        "ORDER BY updated_ts DESC LIMIT 1",
        (prefix + "%",),
    ).fetchone()
    return row[0] if row else None


# --- semantic recall over past interactions --------------------------------

def good_interactions_with_embeddings(conn, exclude_session=None):
    """Past interactions that had a positive outcome and carry a task embedding.

    'Good' = any recorded outcome with reward > 0 (mirrors reward.is_good without
    importing reward here). Optionally excludes an in-flight session.
    """
    sql = (
        "SELECT DISTINCT i.id, i.task, i.response, i.task_embedding, i.session_id "
        "FROM interactions i JOIN outcomes o ON o.interaction_id = i.id "
        "WHERE o.reward > 0 AND i.task_embedding IS NOT NULL"
    )
    params = ()
    if exclude_session:
        sql += " AND (i.session_id IS NULL OR i.session_id != ?)"
        params = (exclude_session,)
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# --- project facts ---------------------------------------------------------

def add_fact(conn, fact_id, project, text, embedding=None):
    conn.execute(
        "INSERT INTO facts(id, project, text, embedding) VALUES(?, ?, ?, ?)",
        (fact_id, project, text, embedding),
    )
    conn.commit()


def facts_for_project(conn, project):
    rows = conn.execute(
        "SELECT id, project, text, embedding FROM facts WHERE project=? "
        "ORDER BY ts ASC, rowid ASC",
        (project,),
    ).fetchall()
    return [dict(r) for r in rows]


def count_facts(conn, project):
    return conn.execute(
        "SELECT COUNT(*) FROM facts WHERE project=?", (project,)
    ).fetchone()[0]
