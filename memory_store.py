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
"""


def connect(path=":memory:", check_same_thread=True):
    conn = sqlite3.connect(path, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    init_db(conn)
    return conn


def init_db(conn):
    conn.executescript(_SCHEMA)
    conn.commit()


def new_id():
    return os.urandom(8).hex()


def log_interaction(conn, interaction_id, task, retrieved_ctx, response, tier):
    conn.execute(
        "INSERT INTO interactions(id, task, retrieved_ctx, response, tier) "
        "VALUES(?, ?, ?, ?, ?)",
        (interaction_id, task, retrieved_ctx, response, tier),
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
