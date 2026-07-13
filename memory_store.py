"""SQLite-backed memory for the sonder learning loop. Stdlib only."""
import array
import math
import os
import re
import sqlite3

import reward

_SCHEMA = """
CREATE TABLE IF NOT EXISTS interactions (
    id TEXT PRIMARY KEY,
    task TEXT,
    retrieved_ctx TEXT,
    response TEXT,
    tier TEXT,
    project TEXT,
    project_explicit INTEGER NOT NULL DEFAULT 1 CHECK(project_explicit IN (0, 1)),
    ts TEXT DEFAULT CURRENT_TIMESTAMP,
    tokens_in INTEGER,
    tokens_out INTEGER,
    token_source TEXT
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
    embedding_model TEXT,
    embedding_revision TEXT,
    embedding_dim INTEGER,
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
CREATE TABLE IF NOT EXISTS session_project_summaries (
    session_id TEXT NOT NULL,
    project_key TEXT NOT NULL,
    summary TEXT,
    summarized_through TEXT,
    updated_ts TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(session_id, project_key)
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
    outcome_ts TEXT,
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
CREATE TABLE IF NOT EXISTS preferences (
    id TEXT PRIMARY KEY,
    scope TEXT DEFAULT 'global',
    key TEXT NOT NULL,
    text TEXT NOT NULL,
    source_interaction TEXT,
    confidence REAL DEFAULT 0.5,
    evidence_count INTEGER DEFAULT 1,
    enabled INTEGER DEFAULT 1,
    created_ts TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_ts TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(scope, key)
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
    if "tokens_in" not in cols:
        conn.execute("ALTER TABLE interactions ADD COLUMN tokens_in INTEGER")
    if "tokens_out" not in cols:
        conn.execute("ALTER TABLE interactions ADD COLUMN tokens_out INTEGER")
    if "token_source" not in cols:
        conn.execute("ALTER TABLE interactions ADD COLUMN token_source TEXT")
    if "project" not in cols:
        conn.execute("ALTER TABLE interactions ADD COLUMN project TEXT")
    if "project_explicit" not in cols:
        conn.execute(
            "ALTER TABLE interactions ADD COLUMN project_explicit INTEGER "
            "NOT NULL DEFAULT 0 CHECK(project_explicit IN (0, 1))"
        )
    if "task_embedding_model" not in cols:
        conn.execute("ALTER TABLE interactions ADD COLUMN task_embedding_model TEXT")
    if "task_embedding_revision" not in cols:
        conn.execute("ALTER TABLE interactions ADD COLUMN task_embedding_revision TEXT")
    task_embedding_dim_added = "task_embedding_dim" not in cols
    if task_embedding_dim_added:
        conn.execute("ALTER TABLE interactions ADD COLUMN task_embedding_dim INTEGER")
    lesson_cols = _column_names(conn, "lessons")
    if "embedding_model" not in lesson_cols:
        conn.execute("ALTER TABLE lessons ADD COLUMN embedding_model TEXT")
    if "embedding_revision" not in lesson_cols:
        conn.execute("ALTER TABLE lessons ADD COLUMN embedding_revision TEXT")
    lesson_embedding_dim_added = "embedding_dim" not in lesson_cols
    if lesson_embedding_dim_added:
        conn.execute("ALTER TABLE lessons ADD COLUMN embedding_dim INTEGER")
    if task_embedding_dim_added:
        conn.execute(
            "UPDATE interactions SET task_embedding_dim=length(task_embedding)/4 "
            "WHERE task_embedding IS NOT NULL AND task_embedding_dim IS NULL "
            "AND length(task_embedding) > 0 AND length(task_embedding) % 4 = 0"
        )
    if lesson_embedding_dim_added:
        conn.execute(
            "UPDATE lessons SET embedding_dim=length(embedding)/4 "
            "WHERE embedding IS NOT NULL AND embedding_dim IS NULL "
            "AND length(embedding) > 0 AND length(embedding) % 4 = 0"
        )
    usage_cols = _column_names(conn, "lesson_usage")
    if "outcome_ts" not in usage_cols:
        conn.execute("ALTER TABLE lesson_usage ADD COLUMN outcome_ts TEXT")


def init_db(conn):
    try:
        # executescript commits any existing transaction first, so begin the
        # write lock inside the script before any schema snapshot/migration.
        conn.executescript("BEGIN IMMEDIATE;\n" + _SCHEMA)
        _migrate(conn)
        # Indexes reference migrated columns, so they must come after _migrate.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_interactions_session "
            "ON interactions(session_id, ts)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_interactions_project "
            "ON interactions(project, ts)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_outcomes_interaction_signal_reward "
            "ON outcomes(interaction_id, signal, reward)"
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
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_preferences_scope_enabled "
            "ON preferences(scope, enabled, updated_ts)"
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def new_id():
    return os.urandom(8).hex()


def _clean_token_count(value):
    if value is None:
        return None
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _estimate_tokens_from_chars(chars):
    chars = max(0, int(chars or 0))
    return (chars + 3) // 4 if chars else 0


def estimate_interaction_tokens(task, retrieved_ctx, response):
    tokens_in = _estimate_tokens_from_chars(
        len(task or "") + len(retrieved_ctx or "")
    )
    tokens_out = _estimate_tokens_from_chars(len(response or ""))
    return tokens_in, tokens_out


def log_interaction(conn, interaction_id, task, retrieved_ctx, response, tier,
                    session_id=None, task_embedding=None, tokens_in=None,
                    tokens_out=None, token_source=None, project=None,
                    project_explicit=True,
                    task_embedding_model=None, task_embedding_revision=None,
                    task_embedding_dim=None):
    tokens_in = _clean_token_count(tokens_in)
    tokens_out = _clean_token_count(tokens_out)
    if token_source is None and (tokens_in is not None or tokens_out is not None):
        token_source = "provided"
    if task_embedding is not None:
        actual_dimension, blob_error = _embedding_blob_integrity(task_embedding)
        if blob_error is not None:
            raise ValueError(
                "task embedding must be a finite non-zero float32 vector"
            )
        if task_embedding_dim is None:
            task_embedding_dim = actual_dimension
        stored_dimension, metadata_error = _stored_embedding_dimension(
            task_embedding_dim
        )
        if metadata_error is not None or stored_dimension != actual_dimension:
            raise ValueError("task embedding dimension does not match blob")
        task_embedding_dim = stored_dimension
    conn.execute(
        "INSERT INTO interactions"
        "(id, task, retrieved_ctx, response, tier, session_id, task_embedding, "
        "tokens_in, tokens_out, token_source, project, project_explicit, "
        "task_embedding_model, "
        "task_embedding_revision, task_embedding_dim) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            interaction_id, task, retrieved_ctx, response, tier, session_id,
            task_embedding, tokens_in, tokens_out, token_source,
            project, int(bool(project_explicit)), task_embedding_model,
            task_embedding_revision or None,
            task_embedding_dim,
        ),
    )
    conn.commit()


def delete_interaction(conn, interaction_id):
    """Remove a captured interaction and its learning traces.

    Used to purge replies that must never influence learning (e.g. a model
    refusal that wrongly denied web access while web tools were enabled)."""
    conn.execute(
        "DELETE FROM outcomes WHERE interaction_id=?", (interaction_id,)
    )
    conn.execute(
        "DELETE FROM lesson_usage WHERE interaction_id=?", (interaction_id,)
    )
    conn.execute("DELETE FROM interactions WHERE id=?", (interaction_id,))
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


def add_lesson(
    conn, lesson_id, text, embedding, source_interaction,
    embedding_model=None, embedding_revision=None, embedding_dim=None,
):
    if embedding is not None:
        actual_dimension, blob_error = _embedding_blob_integrity(embedding)
        if blob_error is not None:
            raise ValueError(
                "lesson embedding must be a finite non-zero float32 vector"
            )
        if embedding_dim is None:
            embedding_dim = actual_dimension
        stored_dimension, metadata_error = _stored_embedding_dimension(
            embedding_dim
        )
        if metadata_error is not None or stored_dimension != actual_dimension:
            raise ValueError("lesson embedding dimension does not match blob")
        embedding_dim = stored_dimension
    conn.execute(
        "INSERT INTO lessons(id, text, embedding, source_interaction, "
        "embedding_model, embedding_revision, embedding_dim) "
        "VALUES(?, ?, ?, ?, ?, ?, ?)",
        (
            lesson_id, text, embedding, source_interaction,
            embedding_model, embedding_revision or None, embedding_dim,
        ),
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
    rows = conn.execute(
        "SELECT id, text, embedding, embedding_model, embedding_revision, "
        "embedding_dim FROM lessons"
    ).fetchall()
    return [dict(r) for r in rows]


def lessons_without_embeddings(conn, limit=100):
    limit = max(1, min(int(limit or 100), 500))
    rows = conn.execute(
        "SELECT id, text, source_interaction, ts FROM lessons "
        "WHERE embedding IS NULL ORDER BY ts ASC, rowid ASC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def set_lesson_embedding(
    conn, lesson_id, embedding, model=None, revision=None, dimension=None,
):
    """Set one missing embedding without overwriting an existing vector."""
    actual_dimension, blob_error = _embedding_blob_integrity(embedding)
    if blob_error is not None:
        raise ValueError("embedding must be a finite non-zero float32 vector")
    if dimension is None and len(embedding) % 4 == 0:
        dimension = actual_dimension
    stored_dimension, metadata_error = _stored_embedding_dimension(dimension)
    if metadata_error is not None or stored_dimension != actual_dimension:
        raise ValueError("embedding dimension does not match blob")
    dimension = stored_dimension
    cur = conn.execute(
        "UPDATE lessons SET embedding=?, embedding_model=?, "
        "embedding_revision=?, embedding_dim=? "
        "WHERE id=? AND embedding IS NULL",
        (embedding, model, revision, dimension, lesson_id),
    )
    conn.commit()
    return cur.rowcount > 0


def _embedding_blob_integrity(blob):
    """Return ``(actual_dimension, error)`` for a stored float32 vector.

    Metadata is deliberately ignored: callers use the bytes as the source of
    truth so a plausible ``embedding_dim`` cannot hide a truncated or NaN
    vector. ``actual_dimension`` remains available for non-finite vectors whose
    byte shape is otherwise valid.
    """
    if blob is None:
        return None, "missing"
    if not isinstance(blob, (bytes, bytearray, memoryview)):
        return None, "invalid_type"
    raw = bytes(blob)
    if not raw or len(raw) % 4:
        return None, "invalid_length"
    values = array.array("f")
    try:
        values.frombytes(raw)
    except (BufferError, ValueError, TypeError):
        return None, "invalid_length"
    dimension = len(values)
    if not dimension:
        return None, "invalid_length"
    if any(not math.isfinite(value) for value in values):
        return dimension, "nonfinite"
    if not any(value != 0.0 for value in values):
        return dimension, "zero_norm"
    return dimension, None


def _stored_embedding_dimension(value):
    if value is None:
        return None, "missing"
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None, "invalid"
    return value, None


def _expected_embedding_dimension(value):
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("embedding dimension must be a positive integer") from exc
    if parsed <= 0 or (isinstance(value, float) and value != parsed):
        raise ValueError("embedding dimension must be a positive integer")
    return parsed


def _embedding_row_needs_refresh(row, model, revision=None, dimension=None):
    """Whether a lesson row is missing, stale, or unsafe for vector recall."""
    expected_dimension = _expected_embedding_dimension(dimension)
    actual_dimension, blob_error = _embedding_blob_integrity(row["embedding"])
    if blob_error is not None:
        return True
    if not row["embedding_model"] or row["embedding_model"] != model:
        return True
    if (
        revision is not None
        and (row["embedding_revision"] or None) != (revision or None)
    ):
        return True
    stored_dimension, metadata_error = _stored_embedding_dimension(
        row["embedding_dim"]
    )
    if metadata_error is not None or stored_dimension != actual_dimension:
        return True
    return (
        expected_dimension is not None
        and actual_dimension != expected_dimension
    )


def lessons_needing_embedding_refresh(
    conn, model, revision=None, dimension=None, limit=100,
):
    """Missing, legacy, or incompatible lesson vectors in stable order."""
    limit = max(1, min(int(limit or 100), 500))
    rows = conn.execute(
        "SELECT id, text, source_interaction, ts, embedding, embedding_model, "
        "embedding_revision, embedding_dim FROM lessons "
        "ORDER BY ts ASC, rowid ASC"
    )
    selected = []
    for row in rows:
        if _embedding_row_needs_refresh(
            row, model, revision=revision, dimension=dimension,
        ):
            selected.append(dict(row))
            if len(selected) >= limit:
                break
    return selected


def count_lessons_needing_embedding_refresh(
    conn, model, revision=None, dimension=None,
):
    rows = conn.execute(
        "SELECT embedding, embedding_model, embedding_revision, embedding_dim "
        "FROM lessons ORDER BY rowid ASC"
    )
    return sum(
        1 for row in rows
        if _embedding_row_needs_refresh(
            row, model, revision=revision, dimension=dimension,
        )
    )


def refresh_lesson_embedding(
    conn, lesson_id, embedding, model, revision=None, dimension=None,
    expected=None,
):
    """Replace one selected vector, optionally only if its old state is unchanged."""
    actual_dimension, blob_error = _embedding_blob_integrity(embedding)
    if blob_error is not None:
        raise ValueError("embedding must be a finite non-zero float32 vector")
    if dimension is not None:
        stored_dimension, metadata_error = _stored_embedding_dimension(dimension)
        if metadata_error is not None or stored_dimension != actual_dimension:
            raise ValueError("embedding dimension does not match blob")
    sql = (
        "UPDATE lessons SET embedding=?, embedding_model=?, "
        "embedding_revision=?, embedding_dim=? WHERE id=?"
    )
    params = [embedding, model, revision or None, actual_dimension, lesson_id]
    if expected is not None:
        sql += (
            " AND embedding IS ? AND embedding_model IS ? "
            "AND embedding_revision IS ? AND embedding_dim IS ? AND text IS ?"
        )
        params.extend([
            expected.get("embedding"), expected.get("embedding_model"),
            expected.get("embedding_revision"), expected.get("embedding_dim"),
            expected.get("text"),
        ])
    cur = conn.execute(sql, tuple(params))
    conn.commit()
    return cur.rowcount > 0


def embedding_provenance_stats(
    conn, model, revision=None, dimension=None,
):
    expected_dimension = _expected_embedding_dimension(dimension)
    rows = conn.execute(
        "SELECT embedding, embedding_model, embedding_revision, embedding_dim "
        "FROM lessons ORDER BY rowid ASC"
    )
    result = {
        "lessons": 0,
        "embedded": 0,
        "valid": 0,
        "missing": 0,
        "vector_invalid": 0,
        "legacy_model": 0,
        "model_mismatch": 0,
        "revision_mismatch": 0,
        "dimension_missing": 0,
        "dimension_invalid": 0,
        "dimension_mismatch": 0,
        "dimensions": {},
    }
    for row in rows:
        result["lessons"] += 1
        actual_dimension, blob_error = _embedding_blob_integrity(row["embedding"])
        if blob_error == "missing":
            result["missing"] += 1
            continue
        result["embedded"] += 1
        dimension_invalid = blob_error in ("invalid_type", "invalid_length")
        if blob_error is None:
            result["valid"] += 1
        elif blob_error in ("nonfinite", "zero_norm"):
            result["vector_invalid"] += 1

        stored_model = row["embedding_model"]
        stored_revision = row["embedding_revision"]
        if not stored_model:
            result["legacy_model"] += 1
        elif stored_model != model:
            result["model_mismatch"] += 1
        if (
            revision is not None
            and stored_model == model
            and (stored_revision or None) != (revision or None)
        ):
            result["revision_mismatch"] += 1

        stored_dimension, metadata_error = _stored_embedding_dimension(
            row["embedding_dim"]
        )
        if metadata_error == "missing":
            result["dimension_missing"] += 1
        elif metadata_error == "invalid":
            dimension_invalid = True
        if dimension_invalid:
            result["dimension_invalid"] += 1

        if actual_dimension is not None:
            key = str(actual_dimension)
            result["dimensions"][key] = result["dimensions"].get(key, 0) + 1
        if (
            actual_dimension is not None
            and stored_dimension is not None
            and stored_dimension != actual_dimension
        ) or (
            actual_dimension is not None
            and expected_dimension is not None
            and stored_model == model
            and actual_dimension != expected_dimension
        ):
            result["dimension_mismatch"] += 1
    return result


def _interaction_task_embedding_needs_refresh(
    row, model, revision=None, dimension=None,
):
    """Whether one stored interaction task vector is unsafe for recall."""
    expected_dimension = _expected_embedding_dimension(dimension)
    actual_dimension, blob_error = _embedding_blob_integrity(
        row["task_embedding"]
    )
    if blob_error is not None:
        return True
    if not row["task_embedding_model"] or row["task_embedding_model"] != model:
        return True
    if (
        revision is not None
        and (row["task_embedding_revision"] or None) != (revision or None)
    ):
        return True
    stored_dimension, metadata_error = _stored_embedding_dimension(
        row["task_embedding_dim"]
    )
    if metadata_error is not None or stored_dimension != actual_dimension:
        return True
    return (
        expected_dimension is not None
        and actual_dimension != expected_dimension
    )


def interactions_needing_task_embedding_refresh(
    conn, model, revision=None, dimension=None, limit=100,
):
    """Return a bounded, stable batch of stale raw-interaction vectors."""
    limit = max(1, min(int(limit or 100), 500))
    rows = conn.execute(
        "SELECT id, task, ts, task_embedding, task_embedding_model, "
        "task_embedding_revision, task_embedding_dim FROM interactions "
        "ORDER BY ts ASC, rowid ASC"
    )
    selected = []
    for row in rows:
        if _interaction_task_embedding_needs_refresh(
            row, model, revision=revision, dimension=dimension,
        ):
            selected.append(dict(row))
            if len(selected) >= limit:
                break
    return selected


def count_interactions_needing_task_embedding_refresh(
    conn, model, revision=None, dimension=None,
):
    """Stream-count task vectors requiring refresh without loading task text."""
    rows = conn.execute(
        "SELECT task_embedding, task_embedding_model, "
        "task_embedding_revision, task_embedding_dim FROM interactions "
        "ORDER BY rowid ASC"
    )
    return sum(
        1 for row in rows
        if _interaction_task_embedding_needs_refresh(
            row, model, revision=revision, dimension=dimension,
        )
    )


def refresh_interaction_task_embedding(
    conn, interaction_id, embedding, model, revision=None, dimension=None,
    expected=None,
):
    """Replace a task vector, optionally only if its old state is unchanged."""
    actual_dimension, blob_error = _embedding_blob_integrity(embedding)
    if blob_error is not None:
        raise ValueError("task embedding must be a finite non-zero float32 vector")
    if dimension is not None:
        stored_dimension, metadata_error = _stored_embedding_dimension(dimension)
        if metadata_error is not None or stored_dimension != actual_dimension:
            raise ValueError("task embedding dimension does not match blob")
    sql = (
        "UPDATE interactions SET task_embedding=?, task_embedding_model=?, "
        "task_embedding_revision=?, task_embedding_dim=? WHERE id=?"
    )
    params = [
        embedding, model, revision or None, actual_dimension, interaction_id,
    ]
    if expected is not None:
        sql += (
            " AND task_embedding IS ? AND task_embedding_model IS ? "
            "AND task_embedding_revision IS ? AND task_embedding_dim IS ? "
            "AND task IS ?"
        )
        params.extend([
            expected.get("task_embedding"),
            expected.get("task_embedding_model"),
            expected.get("task_embedding_revision"),
            expected.get("task_embedding_dim"),
            expected.get("task"),
        ])
    cur = conn.execute(sql, tuple(params))
    conn.commit()
    return cur.rowcount > 0


def interaction_task_embedding_provenance_stats(
    conn, model, revision=None, dimension=None,
):
    """Stream actual-integrity and provenance stats for raw task vectors."""
    expected_dimension = _expected_embedding_dimension(dimension)
    rows = conn.execute(
        "SELECT task_embedding, task_embedding_model, "
        "task_embedding_revision, task_embedding_dim FROM interactions "
        "ORDER BY rowid ASC"
    )
    result = {
        "interactions": 0,
        "embedded": 0,
        "valid": 0,
        "compatible": 0,
        "refresh_required": 0,
        "missing": 0,
        "vector_invalid": 0,
        "legacy_model": 0,
        "model_mismatch": 0,
        "revision_mismatch": 0,
        "dimension_missing": 0,
        "dimension_invalid": 0,
        "dimension_mismatch": 0,
        "dimensions": {},
    }
    for row in rows:
        result["interactions"] += 1
        needs_refresh = _interaction_task_embedding_needs_refresh(
            row, model, revision=revision, dimension=expected_dimension,
        )
        if needs_refresh:
            result["refresh_required"] += 1
        else:
            result["compatible"] += 1

        actual_dimension, blob_error = _embedding_blob_integrity(
            row["task_embedding"]
        )
        if blob_error == "missing":
            result["missing"] += 1
            continue
        result["embedded"] += 1
        dimension_invalid = blob_error in ("invalid_type", "invalid_length")
        if blob_error is None:
            result["valid"] += 1
        elif blob_error in ("nonfinite", "zero_norm"):
            result["vector_invalid"] += 1

        stored_model = row["task_embedding_model"]
        stored_revision = row["task_embedding_revision"]
        if not stored_model:
            result["legacy_model"] += 1
        elif stored_model != model:
            result["model_mismatch"] += 1
        if (
            revision is not None
            and stored_model == model
            and (stored_revision or None) != (revision or None)
        ):
            result["revision_mismatch"] += 1

        stored_dimension, metadata_error = _stored_embedding_dimension(
            row["task_embedding_dim"]
        )
        if metadata_error == "missing":
            result["dimension_missing"] += 1
        elif metadata_error == "invalid":
            dimension_invalid = True
        if dimension_invalid:
            result["dimension_invalid"] += 1

        if actual_dimension is not None:
            key = str(actual_dimension)
            result["dimensions"][key] = (
                result["dimensions"].get(key, 0) + 1
            )
        if (
            actual_dimension is not None
            and stored_dimension is not None
            and stored_dimension != actual_dimension
        ) or (
            actual_dimension is not None
            and expected_dimension is not None
            and stored_model == model
            and actual_dimension != expected_dimension
        ):
            result["dimension_mismatch"] += 1
    return result


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
        "UPDATE lesson_usage SET outcome_signal=?, reward=?, "
        "outcome_ts=CURRENT_TIMESTAMP WHERE interaction_id=?",
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
    stats = {r["lesson_id"]: dict(r) for r in rows}

    # Keep ordered evidence alongside the lifetime counters. Retrieval policy
    # needs to distinguish a lesson that recovered from one that relapsed after
    # an old success; all-history aggregates cannot express that distinction.
    histories = conn.execute(
        "SELECT lesson_id, task, reward, COALESCE(outcome_ts, ts) AS evidence_ts "
        "FROM lesson_usage WHERE reward IS NOT NULL "
        "ORDER BY lesson_id, datetime(evidence_ts), rowid"
    ).fetchall()
    current_id = None
    losses_since_win = 0
    loss_tasks = set()
    rewards_since_win = []
    last_failure_ts = None

    def finish(lesson_id):
        if lesson_id is None or lesson_id not in stats:
            return
        row = stats[lesson_id]
        row["losses_since_win"] = losses_since_win
        row["distinct_loss_tasks_since_win"] = len(loss_tasks)
        row["avg_reward_since_win"] = (
            sum(rewards_since_win) / len(rewards_since_win)
            if rewards_since_win else None
        )
        row["last_failure_ts"] = last_failure_ts

    for evidence in histories:
        lesson_id = evidence["lesson_id"]
        if lesson_id != current_id:
            finish(current_id)
            current_id = lesson_id
            losses_since_win = 0
            loss_tasks = set()
            rewards_since_win = []
            last_failure_ts = None
        value = float(evidence["reward"])
        if value > 0:
            # A grounded success starts a fresh evidence epoch. Later failures
            # can still quarantine the lesson again instead of inheriting
            # permanent immunity from this win.
            losses_since_win = 0
            loss_tasks = set()
            rewards_since_win = []
            last_failure_ts = None
        else:
            rewards_since_win.append(value)
            if value < 0:
                losses_since_win += 1
                normalized_task = re.sub(
                    r"\s+", " ", (evidence["task"] or "").strip().casefold()
                )
                if normalized_task:
                    loss_tasks.add(normalized_task)
                last_failure_ts = evidence["evidence_ts"]
    finish(current_id)

    for row in stats.values():
        row.setdefault("losses_since_win", 0)
        row.setdefault("distinct_loss_tasks_since_win", 0)
        row.setdefault("avg_reward_since_win", None)
        row.setdefault("last_failure_ts", None)
    return stats


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


def interaction_token_totals(conn):
    """Return exact persisted token totals plus estimated fallback for legacy rows."""
    estimated_in_sql = (
        "CASE WHEN (length(COALESCE(task, '')) + length(COALESCE(retrieved_ctx, ''))) = 0 "
        "THEN 0 ELSE ((length(COALESCE(task, '')) + length(COALESCE(retrieved_ctx, '')) + 3) / 4) END"
    )
    estimated_out_sql = (
        "CASE WHEN length(COALESCE(response, '')) = 0 "
        "THEN 0 ELSE ((length(COALESCE(response, '')) + 3) / 4) END"
    )
    row = conn.execute(
        "SELECT "
        "COUNT(*) AS interactions, "
        "SUM(CASE WHEN tokens_in IS NOT NULL OR tokens_out IS NOT NULL THEN 1 ELSE 0 END) AS exact_rows, "
        "SUM(CASE WHEN tokens_in IS NULL AND tokens_out IS NULL THEN 1 ELSE 0 END) AS estimated_rows, "
        "SUM(COALESCE(tokens_in, %s)) AS tokens_in, "
        "SUM(COALESCE(tokens_out, %s)) AS tokens_out "
        "FROM interactions"
        % (estimated_in_sql, estimated_out_sql)
    ).fetchone()
    tokens_in = int(row["tokens_in"] or 0)
    tokens_out = int(row["tokens_out"] or 0)
    return {
        "interactions": int(row["interactions"] or 0),
        "exact_rows": int(row["exact_rows"] or 0),
        "estimated_rows": int(row["estimated_rows"] or 0),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "tokens_total": tokens_in + tokens_out,
    }


def interaction_token_totals_by_tier(conn):
    estimated_in_sql = (
        "CASE WHEN (length(COALESCE(task, '')) + length(COALESCE(retrieved_ctx, ''))) = 0 "
        "THEN 0 ELSE ((length(COALESCE(task, '')) + length(COALESCE(retrieved_ctx, '')) + 3) / 4) END"
    )
    estimated_out_sql = (
        "CASE WHEN length(COALESCE(response, '')) = 0 "
        "THEN 0 ELSE ((length(COALESCE(response, '')) + 3) / 4) END"
    )
    rows = conn.execute(
        "SELECT tier, "
        "COUNT(*) AS interactions, "
        "SUM(CASE WHEN tokens_in IS NOT NULL OR tokens_out IS NOT NULL THEN 1 ELSE 0 END) AS exact_rows, "
        "SUM(CASE WHEN tokens_in IS NULL AND tokens_out IS NULL THEN 1 ELSE 0 END) AS estimated_rows, "
        "SUM(COALESCE(tokens_in, %s)) AS tokens_in, "
        "SUM(COALESCE(tokens_out, %s)) AS tokens_out "
        "FROM interactions GROUP BY tier ORDER BY interactions DESC"
        % (estimated_in_sql, estimated_out_sql)
    ).fetchall()
    out = []
    for row in rows:
        tokens_in = int(row["tokens_in"] or 0)
        tokens_out = int(row["tokens_out"] or 0)
        out.append({
            "tier": row["tier"] or "(unknown)",
            "interactions": int(row["interactions"] or 0),
            "exact_rows": int(row["exact_rows"] or 0),
            "estimated_rows": int(row["estimated_rows"] or 0),
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "tokens_total": tokens_in + tokens_out,
        })
    return out


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
        "WHERE o.signal IN (%s) "
        "ORDER BY i.rowid ASC" % placeholders,
        tuple(sorted(good_signals)),
    ).fetchall()
    return [dict(r) for r in rows]


def interaction_outcome_evidence(conn):
    """Return stable, append-ordered evidence for training-data selection.

    Export policy belongs outside the storage layer, but it needs every outcome
    (including later failures) and stable row identifiers.  Keeping the query
    here prevents callers from accidentally selecting only positive rows and
    overlooking contradictory evidence.
    """
    rows = conn.execute(
        "SELECT i.id, i.task, i.response, i.ts AS interaction_ts, "
        "i.rowid AS interaction_rowid, o.signal, o.reward, "
        "o.ts AS outcome_ts, o.rowid AS outcome_rowid "
        "FROM interactions i JOIN outcomes o ON o.interaction_id=i.id "
        "ORDER BY i.rowid ASC, o.rowid ASC"
    )
    for row in rows:
        yield dict(row)


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


def session_turns_for_project(conn, session_id, project):
    """Session turns with per-turn project provenance matching the request."""
    effective = "NULLIF(i.project,'')"
    sql = (
        "SELECT i.id, i.task, i.response FROM interactions i "
        "WHERE i.session_id=? AND " + effective
    )
    params = [session_id]
    if project is None:
        sql += " IS NULL AND i.project_explicit=1"
    else:
        sql += " = ?"
        params.append(project)
    sql += " ORDER BY i.ts ASC, i.rowid ASC"
    rows = conn.execute(sql, tuple(params)).fetchall()
    return [dict(row) for row in rows]


def ambiguous_legacy_project_turn_count(conn):
    """Sessioned rows that predate trustworthy per-turn project provenance."""
    return int(conn.execute(
        "SELECT COUNT(*) FROM interactions WHERE project_explicit IS NOT 1 "
        "AND session_id IS NOT NULL AND NULLIF(project,'') IS NULL"
    ).fetchone()[0])


def unscoped_session_turn_count(conn):
    """Sessioned turns excluded from every project-scoped history."""
    return int(conn.execute(
        "SELECT COUNT(*) FROM interactions WHERE session_id IS NOT NULL "
        "AND NULLIF(project,'') IS NULL"
    ).fetchone()[0])


def _project_scope_key(project):
    return "none" if project is None else "project:" + str(project)


def get_session_project_summary(conn, session_id, project):
    row = conn.execute(
        "SELECT summary, summarized_through FROM session_project_summaries "
        "WHERE session_id=? AND project_key=?",
        (session_id, _project_scope_key(project)),
    ).fetchone()
    return dict(row) if row else {"summary": None, "summarized_through": None}


def update_session_project_summary(
    conn, session_id, project, summary, summarized_through,
):
    conn.execute(
        "INSERT INTO session_project_summaries"
        "(session_id, project_key, summary, summarized_through) VALUES(?, ?, ?, ?) "
        "ON CONFLICT(session_id, project_key) DO UPDATE SET "
        "summary=excluded.summary, summarized_through=excluded.summarized_through, "
        "updated_ts=CURRENT_TIMESTAMP",
        (
            session_id, _project_scope_key(project), summary,
            summarized_through,
        ),
    )
    conn.commit()


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

def good_interactions_with_embeddings(
    conn, exclude_session=None, project=None, include_all_projects=False,
):
    """Past interactions that had a positive outcome and carry a task embedding.

    Eligibility is fail closed: at least one reward at the grounded-good
    threshold and no weaker/negative outcome. Project is resolved from the
    interaction row only. Ambiguous legacy rows remain unscoped rather than
    inheriting a mutable session label. Cross-project recall requires the
    explicit ``include_all_projects`` override.
    """
    include_all_projects = include_all_projects is True
    good_signals = tuple(sorted(
        signal for signal in reward.VALID_SIGNALS if reward.is_good(signal)
    ))
    placeholders = ",".join("?" for _ in good_signals)
    sql = (
        "SELECT DISTINCT i.id, i.task, i.response, i.task_embedding, i.session_id, "
        "i.task_embedding_model, i.task_embedding_revision, i.task_embedding_dim, "
        "NULLIF(i.project,'') AS project "
        "FROM interactions i JOIN outcomes o ON o.interaction_id = i.id "
        "WHERE o.signal IN (%s) AND o.reward >= ? "
        "AND i.task_embedding IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM outcomes bad "
        "WHERE bad.interaction_id=i.id AND "
        "(bad.signal NOT IN (%s) OR bad.signal IS NULL "
        "OR bad.reward IS NULL OR bad.reward < ?))"
        % (placeholders, placeholders)
    )
    clauses = []
    params = (
        list(good_signals) + [reward.GOOD_THRESHOLD]
        + list(good_signals) + [reward.GOOD_THRESHOLD]
    )
    if exclude_session:
        clauses.append("(i.session_id IS NULL OR i.session_id != ?)")
        params.append(exclude_session)
    if not include_all_projects:
        effective_project = "NULLIF(i.project,'')"
        if project is None:
            clauses.append(
                "%s IS NULL AND (i.project_explicit=1 OR i.session_id IS NULL)"
                % effective_project
            )
        else:
            clauses.append("%s = ?" % effective_project)
            params.append(project)
    if clauses:
        sql += " AND " + " AND ".join(clauses)
    sql += " ORDER BY i.rowid ASC"
    rows = conn.execute(sql, tuple(params)).fetchall()
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


# --- user preferences ------------------------------------------------------

def upsert_preference(conn, pref_id, scope, key, text, source_interaction=None,
                      confidence=0.6):
    scope = (scope or "global").strip() or "global"
    conn.execute(
        "INSERT INTO preferences"
        "(id, scope, key, text, source_interaction, confidence, evidence_count, enabled) "
        "VALUES(?, ?, ?, ?, ?, ?, 1, 1) "
        "ON CONFLICT(scope, key) DO UPDATE SET "
        "text=excluded.text, "
        "source_interaction=COALESCE(excluded.source_interaction, preferences.source_interaction), "
        "confidence=MIN(1.0, MAX(preferences.confidence, excluded.confidence) + 0.05), "
        "evidence_count=preferences.evidence_count + 1, "
        "enabled=1, "
        "updated_ts=CURRENT_TIMESTAMP",
        (pref_id, scope, key, text, source_interaction, float(confidence)),
    )
    conn.commit()


def preferences_for_scope(conn, scope="global", limit=20, include_disabled=False):
    scope = (scope or "global").strip() or "global"
    params = [scope]
    where = "scope=?"
    if not include_disabled:
        where += " AND enabled=1"
    rows = conn.execute(
        "SELECT id, scope, key, text, source_interaction, confidence, "
        "evidence_count, enabled, created_ts, updated_ts "
        "FROM preferences WHERE %s "
        "ORDER BY confidence DESC, evidence_count DESC, updated_ts DESC LIMIT ?"
        % where,
        tuple(params + [int(limit)]),
    ).fetchall()
    return [dict(r) for r in rows]


def task_children(conn, task_id):
    resolved = resolve_task_id(conn, task_id)
    if not resolved:
        return []
    rows = conn.execute(
        "SELECT * FROM tasks WHERE parent_id=? ORDER BY rowid ASC",
        (resolved,),
    ).fetchall()
    return [dict(row) for row in rows]


def all_preferences(conn, limit=50, include_disabled=False):
    where = "" if include_disabled else "WHERE enabled=1"
    rows = conn.execute(
        "SELECT id, scope, key, text, source_interaction, confidence, "
        "evidence_count, enabled, created_ts, updated_ts "
        "FROM preferences %s "
        "ORDER BY scope ASC, confidence DESC, evidence_count DESC, updated_ts DESC LIMIT ?"
        % where,
        (int(limit),),
    ).fetchall()
    return [dict(r) for r in rows]


def set_preference_enabled(conn, pref_id_or_key, enabled, scope="global"):
    scope = (scope or "global").strip() or "global"
    cur = conn.execute(
        "UPDATE preferences SET enabled=?, updated_ts=CURRENT_TIMESTAMP "
        "WHERE id=? OR (scope=? AND key=?)",
        (1 if enabled else 0, pref_id_or_key, scope, pref_id_or_key),
    )
    conn.commit()
    return cur.rowcount
