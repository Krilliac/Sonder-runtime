"""SQLite-backed memory for the sonder learning loop. Stdlib only."""
import array
import math
import os
import re
import sqlite3
import threading
import time

import process_liveness
import reward


_ABANDONED_SESSION_CLAIMS_LOCK = globals().get(
    "_ABANDONED_SESSION_CLAIMS_LOCK", threading.RLock()
)
_ABANDONED_SESSION_CLAIMS = globals().get(
    "_ABANDONED_SESSION_CLAIMS", set()
)
_ABANDONED_DISTILLATION_CLAIMS_LOCK = globals().get(
    "_ABANDONED_DISTILLATION_CLAIMS_LOCK", threading.RLock()
)
_ABANDONED_DISTILLATION_CLAIMS = globals().get(
    "_ABANDONED_DISTILLATION_CLAIMS", set()
)

DISTILLATION_CLAIMED = "claimed"
DISTILLATION_RETRYABLE = "retryable"
DISTILLATION_STORED = "stored"
DISTILLATION_NO_LESSON = "no_lesson"
DISTILLATION_LEGACY_NO_LESSON = "legacy_no_lesson"
DISTILLATION_CANCELLED = "cancelled"

_DISTILLATION_LIVE_STATES = frozenset({
    DISTILLATION_CLAIMED,
    DISTILLATION_RETRYABLE,
})
_DISTILLATION_TERMINAL_STATES = frozenset({
    DISTILLATION_STORED,
    DISTILLATION_NO_LESSON,
    DISTILLATION_LEGACY_NO_LESSON,
    DISTILLATION_CANCELLED,
})
_DISTILLATION_BACKFILL_MIGRATION = "lesson_distillations_v1_backfill"

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
CREATE TABLE IF NOT EXISTS memory_migrations (
    name TEXT PRIMARY KEY,
    applied_ts TEXT DEFAULT CURRENT_TIMESTAMP
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
CREATE TABLE IF NOT EXISTS session_turn_claims (
    session_id TEXT PRIMARY KEY,
    claim_token TEXT NOT NULL,
    owner_pid INTEGER NOT NULL,
    owner_identity TEXT NOT NULL,
    claimed_at REAL NOT NULL
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
CREATE TABLE IF NOT EXISTS lesson_distillations (
    interaction_id TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK(state IN (
        'claimed', 'retryable', 'stored', 'no_lesson',
        'legacy_no_lesson', 'cancelled'
    )),
    signal TEXT,
    claim_token TEXT,
    owner_pid INTEGER,
    owner_identity TEXT,
    claimed_at REAL,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    last_error TEXT,
    created_ts TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_ts TEXT DEFAULT CURRENT_TIMESTAMP,
    completed_ts TEXT,
    CHECK (
        (state = 'claimed' AND claim_token IS NOT NULL
         AND owner_pid IS NOT NULL AND owner_identity IS NOT NULL
         AND claimed_at IS NOT NULL)
        OR
        (state != 'claimed' AND claim_token IS NULL
         AND owner_pid IS NULL AND owner_identity IS NULL
         AND claimed_at IS NULL)
    )
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


def _good_outcome_signals():
    return tuple(sorted(
        signal for signal in reward.VALID_SIGNALS if reward.is_good(signal)
    ))


def _dedupe_outcomes_for_unique_index(conn):
    """Keep the earliest append for each non-null interaction/signal pair."""
    index_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
        ("uq_outcomes_interaction_signal_nonnull",),
    ).fetchone()
    if index_exists is not None:
        return
    conn.execute(
        "DELETE FROM outcomes WHERE interaction_id IS NOT NULL "
        "AND signal IS NOT NULL AND rowid NOT IN ("
        "SELECT MIN(rowid) FROM outcomes "
        "WHERE interaction_id IS NOT NULL AND signal IS NOT NULL "
        "GROUP BY interaction_id, signal)"
    )


def _backfill_lesson_distillations_once(conn):
    """Mark legacy good outcomes terminal without invoking a model or embedder."""
    already_applied = conn.execute(
        "SELECT 1 FROM memory_migrations WHERE name=?",
        (_DISTILLATION_BACKFILL_MIGRATION,),
    ).fetchone()
    if already_applied is not None:
        return

    good_signals = _good_outcome_signals()
    if good_signals:
        placeholders = ",".join("?" for _ in good_signals)
        conn.execute(
            "INSERT OR IGNORE INTO lesson_distillations("
            "interaction_id, state, signal, attempts, completed_ts) "
            "SELECT i.id, CASE WHEN EXISTS ("
            "SELECT 1 FROM lessons l WHERE l.source_interaction=i.id"
            ") THEN ? ELSE ? END, ("
            "SELECT o2.signal FROM outcomes o2 "
            "WHERE o2.interaction_id=i.id AND o2.signal IN (%s) "
            "AND o2.reward >= ? ORDER BY o2.rowid ASC LIMIT 1"
            "), 0, CURRENT_TIMESTAMP FROM interactions i "
            "WHERE EXISTS (SELECT 1 FROM outcomes o "
            "WHERE o.interaction_id=i.id AND o.signal IN (%s) "
            "AND o.reward >= ?)" % (placeholders, placeholders),
            (
                DISTILLATION_STORED,
                DISTILLATION_LEGACY_NO_LESSON,
                *good_signals,
                reward.GOOD_THRESHOLD,
                *good_signals,
                reward.GOOD_THRESHOLD,
            ),
        )
    conn.execute(
        "INSERT INTO memory_migrations(name) VALUES(?)",
        (_DISTILLATION_BACKFILL_MIGRATION,),
    )


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
    _dedupe_outcomes_for_unique_index(conn)
    _backfill_lesson_distillations_once(conn)
    claim_cols = _column_names(conn, "session_turn_claims")
    if "owner_pid" not in claim_cols or "owner_identity" not in claim_cols:
        # Claims are ephemeral coordination state, so replacing the old
        # lease-based shape is safer than trying to preserve stale ownership.
        conn.execute("DROP TABLE session_turn_claims")
        conn.execute(
            "CREATE TABLE session_turn_claims ("
            "session_id TEXT PRIMARY KEY, claim_token TEXT NOT NULL, "
            "owner_pid INTEGER NOT NULL, owner_identity TEXT NOT NULL, "
            "claimed_at REAL NOT NULL)"
        )


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
        # A single-column index preserves outcome rowid order for each
        # interaction, allowing bounded interaction-first evidence streaming
        # without SQLite materializing the complete join in a temp B-tree.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_outcomes_interaction "
            "ON outcomes(interaction_id)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "uq_outcomes_interaction_signal_nonnull "
            "ON outcomes(interaction_id, signal) "
            "WHERE interaction_id IS NOT NULL AND signal IS NOT NULL"
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
    conn.execute(
        "DELETE FROM lesson_distillations WHERE interaction_id=?",
        (interaction_id,),
    )
    conn.execute("DELETE FROM interactions WHERE id=?", (interaction_id,))
    conn.commit()


def get_interaction(conn, interaction_id):
    row = conn.execute(
        "SELECT * FROM interactions WHERE id=?", (interaction_id,)
    ).fetchone()
    return dict(row) if row else None


def claim_session_turn(
    conn, session_id, claim_token, *, owner_pid=None, now=None,
    owner_identity=None, owner_probe=None,
):
    """Claim a session until its token is released or its owner process dies."""
    session_id = str(session_id or "").strip()
    claim_token = str(claim_token or "").strip()
    if not session_id or not claim_token:
        return False
    owner_pid = os.getpid() if owner_pid is None else int(owner_pid)
    if owner_pid <= 0:
        return False
    owner_probe = owner_probe or process_liveness.probe_process
    if owner_identity is None:
        owner_state, owner_identity = owner_probe(owner_pid)
    else:
        owner_state, _actual_identity = owner_probe(
            owner_pid, expected_identity=owner_identity,
        )
    if owner_state != process_liveness.PROCESS_ALIVE or not owner_identity:
        return False
    owner_identity = str(owner_identity).strip()
    if not owner_identity:
        return False
    current = time.time() if now is None else float(now)
    reclaimed_token = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT claim_token, owner_pid, owner_identity "
            "FROM session_turn_claims "
            "WHERE session_id=?",
            (session_id,),
        ).fetchone()
        if existing is not None:
            existing_state, _actual_identity = owner_probe(
                existing["owner_pid"],
                expected_identity=existing["owner_identity"],
            )
            existing_marker = (
                session_id,
                existing["claim_token"],
                existing["owner_pid"],
                existing["owner_identity"],
            )
            with _ABANDONED_SESSION_CLAIMS_LOCK:
                abandoned = existing_marker in _ABANDONED_SESSION_CLAIMS
            same_owner = (
                existing["owner_pid"] == owner_pid
                and existing["owner_identity"] == owner_identity
            )
            if existing_state != process_liveness.PROCESS_DEAD and not (
                same_owner and abandoned
            ):
                conn.commit()
                return False
            conn.execute(
                "DELETE FROM session_turn_claims WHERE session_id=? "
                "AND claim_token=? AND owner_pid=? AND owner_identity=?",
                (
                    session_id, existing["claim_token"], existing["owner_pid"],
                    existing["owner_identity"],
                ),
            )
            reclaimed_token = existing["claim_token"]
        cur = conn.execute(
            "INSERT INTO session_turn_claims"
            "(session_id, claim_token, owner_pid, owner_identity, claimed_at) "
            "VALUES(?, ?, ?, ?, ?) ON CONFLICT(session_id) DO NOTHING",
            (session_id, claim_token, owner_pid, owner_identity, current),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    if reclaimed_token is not None:
        with _ABANDONED_SESSION_CLAIMS_LOCK:
            _ABANDONED_SESSION_CLAIMS.discard(existing_marker)
    return cur.rowcount == 1


def release_session_turn(conn, session_id, claim_token):
    """Release a session claim without allowing a stale owner to clear a new one."""
    try:
        cur = conn.execute(
            "DELETE FROM session_turn_claims "
            "WHERE session_id=? AND claim_token=?",
            (session_id, claim_token),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    if cur.rowcount == 1:
        with _ABANDONED_SESSION_CLAIMS_LOCK:
            stale_markers = {
                marker for marker in _ABANDONED_SESSION_CLAIMS
                if isinstance(marker, tuple) and len(marker) == 4
                and marker[0] == session_id and marker[1] == claim_token
            }
            _ABANDONED_SESSION_CLAIMS.difference_update(stale_markers)
    return cur.rowcount == 1


def abandon_session_turn_claim(
    session_id, claim_token, owner_pid, owner_identity,
):
    """Mark a completed same-process claim reclaimable after release I/O failure."""
    session_id = str(session_id or "").strip()
    claim_token = str(claim_token or "").strip()
    owner_identity = str(owner_identity or "").strip()
    try:
        owner_pid = int(owner_pid)
    except (TypeError, ValueError, OverflowError):
        return False
    if not session_id or not claim_token or owner_pid <= 0 or not owner_identity:
        return False
    marker = (session_id, claim_token, owner_pid, owner_identity)
    with _ABANDONED_SESSION_CLAIMS_LOCK:
        _ABANDONED_SESSION_CLAIMS.add(marker)
    return True


def replace_interaction_response_cas(
    conn, interaction_id, *, expected, response, tokens_in, tokens_out,
    token_source,
):
    """Replace a captured response only while its learning state is unchanged."""
    if not isinstance(expected, dict) or expected.get("id") != interaction_id:
        return False
    try:
        cur = conn.execute(
            "UPDATE interactions SET response=?, tokens_in=?, tokens_out=?, "
            "token_source=? WHERE id=? "
            "AND response IS ? AND tokens_in IS ? AND tokens_out IS ? "
            "AND token_source IS ? AND task IS ? AND retrieved_ctx IS ? "
            "AND tier IS ? AND session_id IS ? AND project IS ? "
            "AND project_explicit IS ? "
            "AND NOT EXISTS ("
            "SELECT 1 FROM outcomes "
            "WHERE outcomes.interaction_id=interactions.id"
            ") AND NOT EXISTS ("
            "SELECT 1 FROM lessons "
            "WHERE lessons.source_interaction=interactions.id"
            ") AND NOT EXISTS ("
            "SELECT 1 FROM lesson_usage "
            "WHERE lesson_usage.interaction_id=interactions.id "
            "AND lesson_usage.outcome_signal IS NOT NULL"
            ")",
            (
                response, tokens_in, tokens_out, token_source, interaction_id,
                expected["response"], expected["tokens_in"],
                expected["tokens_out"], expected["token_source"],
                expected["task"], expected["retrieved_ctx"], expected["tier"],
                expected["session_id"], expected["project"],
                expected["project_explicit"],
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return cur.rowcount == 1


def record_outcome_row(conn, interaction_id, signal, reward_value):
    """Record one signal once; return whether this call inserted the evidence."""
    signal = str(signal or "").strip()
    if signal not in reward.VALID_SIGNALS:
        raise ValueError("signal is not a supported grounded outcome")
    if isinstance(reward_value, bool):
        raise ValueError("reward must match the canonical signal reward")
    try:
        reward_value = float(reward_value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("reward must match the canonical signal reward") from exc
    canonical = reward.score(signal)
    if not math.isfinite(reward_value) or reward_value != canonical:
        raise ValueError("reward must match the canonical signal reward")
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO outcomes(interaction_id, signal, reward) "
            "VALUES(?, ?, ?)",
            (interaction_id, signal, canonical),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return cur.rowcount == 1


def _distillation_owner(owner_pid, owner_identity, owner_probe):
    """Return a verified process-instance tuple or None (UNKNOWN fails closed)."""
    if isinstance(owner_pid, bool):
        return None
    try:
        owner_pid = os.getpid() if owner_pid is None else int(owner_pid)
    except (TypeError, ValueError, OverflowError):
        return None
    if owner_pid <= 0:
        return None
    owner_probe = owner_probe or process_liveness.probe_process
    owner_identity = str(owner_identity or "").strip() or None
    try:
        if owner_identity is None:
            owner_state, owner_identity = owner_probe(owner_pid)
        else:
            owner_state, _actual_identity = owner_probe(
                owner_pid, expected_identity=owner_identity,
            )
    except Exception:
        return None
    owner_identity = str(owner_identity or "").strip()
    if (
        owner_state != process_liveness.PROCESS_ALIVE
        or not owner_identity
    ):
        return None
    return owner_pid, owner_identity


def abandon_lesson_distillation_claim(
    interaction_id, claim_token, owner_pid, owner_identity,
):
    """Mark an exact same-process claim reclaimable after release I/O failure."""
    interaction_id = str(interaction_id or "").strip()
    claim_token = str(claim_token or "").strip()
    owner_identity = str(owner_identity or "").strip()
    try:
        owner_pid = int(owner_pid)
    except (TypeError, ValueError, OverflowError):
        return False
    if not interaction_id or not claim_token or owner_pid <= 0 or not owner_identity:
        return False
    marker = (interaction_id, claim_token, owner_pid, owner_identity)
    with _ABANDONED_DISTILLATION_CLAIMS_LOCK:
        _ABANDONED_DISTILLATION_CLAIMS.add(marker)
    return True


def _consume_abandoned_distillation_claim(row, interaction_id, owner):
    if row is None or row["state"] != DISTILLATION_CLAIMED:
        return False
    marker = (
        interaction_id, row["claim_token"], row["owner_pid"],
        row["owner_identity"],
    )
    if owner is not None and owner != (row["owner_pid"], row["owner_identity"]):
        return False
    with _ABANDONED_DISTILLATION_CLAIMS_LOCK:
        if marker not in _ABANDONED_DISTILLATION_CLAIMS:
            return False
        _ABANDONED_DISTILLATION_CLAIMS.discard(marker)
    return True


def _discard_abandoned_distillation_claims(interaction_id):
    with _ABANDONED_DISTILLATION_CLAIMS_LOCK:
        stale = {
            marker for marker in _ABANDONED_DISTILLATION_CLAIMS
            if isinstance(marker, tuple) and len(marker) == 4
            and marker[0] == interaction_id
        }
        _ABANDONED_DISTILLATION_CLAIMS.difference_update(stale)


def _distillation_evidence(conn, interaction_id):
    """Return (has_grounded_good, has_contradiction) from persisted evidence."""
    good_signals = _good_outcome_signals()
    if not good_signals:
        has_any = conn.execute(
            "SELECT 1 FROM outcomes WHERE interaction_id=? LIMIT 1",
            (interaction_id,),
        ).fetchone()
        return False, has_any is not None
    placeholders = ",".join("?" for _ in good_signals)
    row = conn.execute(
        "SELECT EXISTS(SELECT 1 FROM outcomes good "
        "WHERE good.interaction_id=? AND good.signal IN (%s) "
        "AND good.reward >= ?) AS has_good, "
        "EXISTS(SELECT 1 FROM outcomes bad "
        "WHERE bad.interaction_id=? AND (bad.signal IS NULL "
        "OR bad.signal NOT IN (%s) OR bad.reward IS NULL "
        "OR bad.reward < ?)) AS has_contradiction"
        % (placeholders, placeholders),
        (
            interaction_id, *good_signals, reward.GOOD_THRESHOLD,
            interaction_id, *good_signals, reward.GOOD_THRESHOLD,
        ),
    ).fetchone()
    return bool(row["has_good"]), bool(row["has_contradiction"])


def _distillation_row(conn, interaction_id):
    return conn.execute(
        "SELECT * FROM lesson_distillations WHERE interaction_id=?",
        (interaction_id,),
    ).fetchone()


def _cancel_live_distillation(conn, interaction_id, signal, reason):
    row = _distillation_row(conn, interaction_id)
    if row is None:
        conn.execute(
            "INSERT INTO lesson_distillations("
            "interaction_id, state, signal, attempts, last_error, completed_ts) "
            "VALUES(?, ?, ?, 0, ?, CURRENT_TIMESTAMP)",
            (
                interaction_id, DISTILLATION_CANCELLED, signal,
                str(reason or "contradictory outcome"),
            ),
        )
        return True
    if row["state"] not in _DISTILLATION_LIVE_STATES:
        return False
    cur = conn.execute(
        "UPDATE lesson_distillations SET state=?, signal=?, claim_token=NULL, "
        "owner_pid=NULL, owner_identity=NULL, claimed_at=NULL, last_error=?, "
        "updated_ts=CURRENT_TIMESTAMP, completed_ts=CURRENT_TIMESTAMP "
        "WHERE interaction_id=? AND state IN (?, ?)",
        (
            DISTILLATION_CANCELLED, signal,
            str(reason or "contradictory outcome"), interaction_id,
            DISTILLATION_CLAIMED, DISTILLATION_RETRYABLE,
        ),
    )
    return cur.rowcount == 1


def _claim_distillation(
    conn, interaction_id, signal, claim_token, owner, owner_probe, claimed_at,
):
    """Acquire/recover a job while the caller holds BEGIN IMMEDIATE."""
    row = _distillation_row(conn, interaction_id)
    if row is None:
        _discard_abandoned_distillation_claims(interaction_id)
        if owner is None:
            conn.execute(
                "INSERT INTO lesson_distillations("
                "interaction_id, state, signal, attempts, last_error) "
                "VALUES(?, ?, ?, 0, ?)",
                (
                    interaction_id, DISTILLATION_RETRYABLE, signal,
                    "owner identity unavailable",
                ),
            )
            return False
        owner_pid, owner_identity = owner
        conn.execute(
            "INSERT INTO lesson_distillations("
            "interaction_id, state, signal, claim_token, owner_pid, "
            "owner_identity, claimed_at, attempts) VALUES(?, ?, ?, ?, ?, ?, ?, 1)",
            (
                interaction_id, DISTILLATION_CLAIMED, signal, claim_token,
                owner_pid, owner_identity, claimed_at,
            ),
        )
        return True

    if row["state"] == DISTILLATION_RETRYABLE:
        _discard_abandoned_distillation_claims(interaction_id)
        if owner is None:
            return False
        owner_pid, owner_identity = owner
        cur = conn.execute(
            "UPDATE lesson_distillations SET state=?, signal=?, claim_token=?, "
            "owner_pid=?, owner_identity=?, claimed_at=?, attempts=attempts+1, "
            "last_error=NULL, updated_ts=CURRENT_TIMESTAMP, completed_ts=NULL "
            "WHERE interaction_id=? AND state=?",
            (
                DISTILLATION_CLAIMED, signal, claim_token, owner_pid,
                owner_identity, claimed_at, interaction_id,
                DISTILLATION_RETRYABLE,
            ),
        )
        return cur.rowcount == 1

    if row["state"] != DISTILLATION_CLAIMED:
        _discard_abandoned_distillation_claims(interaction_id)
        return False

    if _consume_abandoned_distillation_claim(row, interaction_id, owner):
        if owner is None:
            conn.execute(
                "UPDATE lesson_distillations SET state=?, claim_token=NULL, "
                "owner_pid=NULL, owner_identity=NULL, claimed_at=NULL, "
                "last_error=?, updated_ts=CURRENT_TIMESTAMP "
                "WHERE interaction_id=? AND state=? AND claim_token=? "
                "AND owner_pid=? AND owner_identity=?",
                (
                    DISTILLATION_RETRYABLE, "same-process claim abandoned",
                    interaction_id, DISTILLATION_CLAIMED, row["claim_token"],
                    row["owner_pid"], row["owner_identity"],
                ),
            )
            return False
        owner_pid, owner_identity = owner
        cur = conn.execute(
            "UPDATE lesson_distillations SET signal=?, claim_token=?, "
            "owner_pid=?, owner_identity=?, claimed_at=?, attempts=attempts+1, "
            "last_error=NULL, updated_ts=CURRENT_TIMESTAMP "
            "WHERE interaction_id=? AND state=? AND claim_token=? "
            "AND owner_pid=? AND owner_identity=?",
            (
                signal, claim_token, owner_pid, owner_identity, claimed_at,
                interaction_id, DISTILLATION_CLAIMED, row["claim_token"],
                row["owner_pid"], row["owner_identity"],
            ),
        )
        return cur.rowcount == 1

    if owner is not None:
        owner_pid, owner_identity = owner
        if (
            row["claim_token"] == claim_token
            and row["owner_pid"] == owner_pid
            and row["owner_identity"] == owner_identity
        ):
            return True

    owner_probe = owner_probe or process_liveness.probe_process
    try:
        existing_state, _actual_identity = owner_probe(
            row["owner_pid"], expected_identity=row["owner_identity"],
        )
    except Exception:
        existing_state = process_liveness.PROCESS_UNKNOWN
    if existing_state != process_liveness.PROCESS_DEAD:
        return False

    if owner is None:
        conn.execute(
            "UPDATE lesson_distillations SET state=?, claim_token=NULL, "
            "owner_pid=NULL, owner_identity=NULL, claimed_at=NULL, last_error=?, "
            "updated_ts=CURRENT_TIMESTAMP WHERE interaction_id=? "
            "AND state=? AND claim_token=? AND owner_pid=? AND owner_identity=?",
            (
                DISTILLATION_RETRYABLE, "previous owner is dead",
                interaction_id, DISTILLATION_CLAIMED, row["claim_token"],
                row["owner_pid"], row["owner_identity"],
            ),
        )
        return False

    owner_pid, owner_identity = owner
    cur = conn.execute(
        "UPDATE lesson_distillations SET signal=?, claim_token=?, owner_pid=?, "
        "owner_identity=?, claimed_at=?, attempts=attempts+1, last_error=NULL, "
        "updated_ts=CURRENT_TIMESTAMP WHERE interaction_id=? AND state=? "
        "AND claim_token=? AND owner_pid=? AND owner_identity=?",
        (
            signal, claim_token, owner_pid, owner_identity, claimed_at,
            interaction_id, DISTILLATION_CLAIMED, row["claim_token"],
            row["owner_pid"], row["owner_identity"],
        ),
    )
    return cur.rowcount == 1


def _outcome_distillation_result(
    row, *, outcome_inserted, usage_rows_updated, claimed, claim_token,
):
    return {
        "outcome_inserted": bool(outcome_inserted),
        "usage_rows_updated": int(usage_rows_updated or 0),
        "distillation_state": row["state"] if row is not None else None,
        "claimed": bool(claimed),
        "claim_token": claim_token if claimed else None,
        "attempts": int(row["attempts"] or 0) if row is not None else 0,
    }


def record_outcome_and_claim_lesson_distillation(
    conn, interaction_id, signal, reward_value, *, claim_token=None,
    owner_pid=None, owner_identity=None, owner_probe=None, now=None,
):
    """Atomically record evidence, credit usage, and claim eligible distillation.

    A duplicate non-null interaction/signal is a storage no-op and does not move
    ``lesson_usage.outcome_ts``. It can still reacquire a retryable job or recover
    one whose exact PID/process-start owner is confirmed dead. UNKNOWN liveness
    never steals a claim. Contradictory evidence cancels only live jobs.
    """
    interaction_id = str(interaction_id or "").strip()
    signal = str(signal or "").strip()
    if not interaction_id or not signal:
        raise ValueError("interaction_id and signal are required")
    try:
        reward_value = float(reward_value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("reward_value must be finite") from exc
    if not math.isfinite(reward_value):
        raise ValueError("reward_value must be finite")
    if signal not in reward.VALID_SIGNALS:
        raise ValueError("signal is not a supported grounded outcome")
    canonical_reward = reward.score(signal)
    if reward_value != canonical_reward:
        raise ValueError("reward_value must match the canonical signal reward")
    reward_value = canonical_reward

    claim_token = str(claim_token or new_id()).strip()
    if not claim_token:
        raise ValueError("claim_token is required")
    owner = _distillation_owner(owner_pid, owner_identity, owner_probe)
    claimed_at = time.time() if now is None else float(now)

    try:
        conn.execute("BEGIN IMMEDIATE")
        interaction_exists = conn.execute(
            "SELECT 1 FROM interactions WHERE id=?", (interaction_id,),
        ).fetchone()
        if interaction_exists is None:
            conn.commit()
            return _outcome_distillation_result(
                None, outcome_inserted=False, usage_rows_updated=0,
                claimed=False, claim_token=claim_token,
            )

        outcome_cur = conn.execute(
            "INSERT OR IGNORE INTO outcomes(interaction_id, signal, reward) "
            "VALUES(?, ?, ?)",
            (interaction_id, signal, reward_value),
        )
        outcome_inserted = outcome_cur.rowcount == 1
        usage_rows_updated = 0
        if outcome_inserted:
            usage_cur = conn.execute(
                "UPDATE lesson_usage SET outcome_signal=?, reward=?, "
                "outcome_ts=CURRENT_TIMESTAMP WHERE interaction_id=?",
                (signal, reward_value, interaction_id),
            )
            usage_rows_updated = usage_cur.rowcount

        has_good, has_contradiction = _distillation_evidence(
            conn, interaction_id,
        )
        claimed = False
        if not has_good or has_contradiction:
            _cancel_live_distillation(
                conn, interaction_id, signal, "contradictory outcome evidence",
            )
        else:
            claimed = _claim_distillation(
                conn, interaction_id, signal, claim_token, owner, owner_probe,
                claimed_at,
            )
        row = _distillation_row(conn, interaction_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return _outcome_distillation_result(
        row, outcome_inserted=outcome_inserted,
        usage_rows_updated=usage_rows_updated, claimed=claimed,
        claim_token=claim_token,
    )


def mark_lesson_distillation_retryable(
    conn, interaction_id, claim_token, error="",
):
    """Release an exact live claim for retry; contradictory evidence cancels it."""
    interaction_id = str(interaction_id or "").strip()
    claim_token = str(claim_token or "").strip()
    if not interaction_id or not claim_token:
        return False
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = _distillation_row(conn, interaction_id)
        if (
            row is None
            or row["state"] != DISTILLATION_CLAIMED
            or row["claim_token"] != claim_token
        ):
            conn.commit()
            return False
        has_good, has_contradiction = _distillation_evidence(
            conn, interaction_id,
        )
        if not has_good or has_contradiction:
            _cancel_live_distillation(
                conn, interaction_id, row["signal"],
                "contradictory outcome evidence",
            )
            conn.commit()
            return False
        cur = conn.execute(
            "UPDATE lesson_distillations SET state=?, claim_token=NULL, "
            "owner_pid=NULL, owner_identity=NULL, claimed_at=NULL, last_error=?, "
            "updated_ts=CURRENT_TIMESTAMP WHERE interaction_id=? AND state=? "
            "AND claim_token=?",
            (
                DISTILLATION_RETRYABLE, str(error or ""), interaction_id,
                DISTILLATION_CLAIMED, claim_token,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return cur.rowcount == 1


def cancel_lesson_distillation(
    conn, interaction_id, reason="", claim_token=None,
):
    """Cancel a live job, optionally requiring its exact active claim token."""
    interaction_id = str(interaction_id or "").strip()
    claim_token = str(claim_token or "").strip() or None
    if not interaction_id:
        return False
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = _distillation_row(conn, interaction_id)
        if row is None or row["state"] not in _DISTILLATION_LIVE_STATES:
            conn.commit()
            return False
        if claim_token is not None and (
            row["state"] != DISTILLATION_CLAIMED
            or row["claim_token"] != claim_token
        ):
            conn.commit()
            return False
        cur = conn.execute(
            "UPDATE lesson_distillations SET state=?, claim_token=NULL, "
            "owner_pid=NULL, owner_identity=NULL, claimed_at=NULL, last_error=?, "
            "updated_ts=CURRENT_TIMESTAMP, completed_ts=CURRENT_TIMESTAMP "
            "WHERE interaction_id=? AND state IN (?, ?)",
            (
                DISTILLATION_CANCELLED, str(reason or "cancelled"),
                interaction_id, DISTILLATION_CLAIMED,
                DISTILLATION_RETRYABLE,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return cur.rowcount == 1


def finalize_lesson_distillation(
    conn, interaction_id, claim_token, transaction_body=None,
):
    """Run locked dedupe/write work and atomically make a claim terminal.

    ``transaction_body(conn)`` must not commit or roll back. It runs under the
    same ``BEGIN IMMEDIATE`` as the terminal transition and returns a mapping
    containing ``terminal_state`` (``stored`` or ``no_lesson``), plus optional
    ``lesson_id`` and ``result`` metadata. Any exception rolls back both lesson
    tables and the ledger. Persisted contradictory evidence cancels the claim
    before the callback runs.
    """
    interaction_id = str(interaction_id or "").strip()
    claim_token = str(claim_token or "").strip()
    if not interaction_id or not claim_token:
        return {
            "finalized": False,
            "distillation_state": None,
            "lesson_id": None,
            "result": None,
        }
    if transaction_body is not None and not callable(transaction_body):
        raise TypeError("transaction_body must be callable")

    try:
        conn.execute("BEGIN IMMEDIATE")
        row = _distillation_row(conn, interaction_id)
        if (
            row is None
            or row["state"] != DISTILLATION_CLAIMED
            or row["claim_token"] != claim_token
        ):
            conn.commit()
            return {
                "finalized": False,
                "distillation_state": row["state"] if row is not None else None,
                "lesson_id": None,
                "result": None,
            }

        has_good, has_contradiction = _distillation_evidence(
            conn, interaction_id,
        )
        if not has_good or has_contradiction:
            _cancel_live_distillation(
                conn, interaction_id, row["signal"],
                "contradictory outcome evidence",
            )
            conn.commit()
            return {
                "finalized": False,
                "distillation_state": DISTILLATION_CANCELLED,
                "lesson_id": None,
                "result": None,
            }

        body_result = (
            transaction_body(conn) if transaction_body is not None
            else {"terminal_state": DISTILLATION_NO_LESSON}
        )
        if body_result is None:
            body_result = {"terminal_state": DISTILLATION_NO_LESSON}
        if not isinstance(body_result, dict):
            raise TypeError("transaction_body must return a mapping or None")
        terminal_state = body_result.get("terminal_state")
        if terminal_state not in (
            DISTILLATION_STORED,
            DISTILLATION_NO_LESSON,
        ):
            raise ValueError(
                "transaction_body terminal_state must be stored or no_lesson"
            )

        lesson_row = conn.execute(
            "SELECT id FROM lessons WHERE source_interaction=? "
            "ORDER BY rowid ASC LIMIT 1",
            (interaction_id,),
        ).fetchone()
        if terminal_state == DISTILLATION_STORED and lesson_row is None:
            raise ValueError("stored finalization requires an interaction lesson")
        if lesson_row is not None:
            terminal_state = DISTILLATION_STORED
        # Report persisted provenance, never unverified callback metadata.
        lesson_id = lesson_row["id"] if lesson_row is not None else None

        cur = conn.execute(
            "UPDATE lesson_distillations SET state=?, claim_token=NULL, "
            "owner_pid=NULL, owner_identity=NULL, claimed_at=NULL, last_error=NULL, "
            "updated_ts=CURRENT_TIMESTAMP, completed_ts=CURRENT_TIMESTAMP "
            "WHERE interaction_id=? AND state=? AND claim_token=?",
            (
                terminal_state, interaction_id, DISTILLATION_CLAIMED,
                claim_token,
            ),
        )
        if cur.rowcount != 1:
            raise RuntimeError("distillation claim changed during finalization")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "finalized": True,
        "distillation_state": terminal_state,
        "lesson_id": lesson_id,
        "result": body_result.get("result", body_result),
    }


def _insert_lesson_rows(
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
            # Preserve the revision string exactly, including the empty string a
            # runtime with no local manifest reports. Coercing "" -> NULL made a
            # successfully-embedded lesson (non-null model + dim) store a NULL
            # revision, contradicting the provenance contract and its dedupe
            # comparison (which still normalizes "" and None together below).
            lesson_id, text, embedding, source_interaction,
            embedding_model, embedding_revision, embedding_dim,
        ),
    )
    conn.execute(
        "INSERT INTO lessons_fts(lesson_id, text) VALUES(?, ?)", (lesson_id, text)
    )
    return lesson_id


def insert_lesson_in_transaction(
    conn, lesson_id, text, embedding, source_interaction,
    embedding_model=None, embedding_revision=None, embedding_dim=None,
):
    """Insert the lesson and FTS mirror without committing an active transaction."""
    if not conn.in_transaction:
        raise RuntimeError("insert_lesson_in_transaction requires an active transaction")
    return _insert_lesson_rows(
        conn, lesson_id, text, embedding, source_interaction,
        embedding_model=embedding_model,
        embedding_revision=embedding_revision,
        embedding_dim=embedding_dim,
    )


def add_lesson(
    conn, lesson_id, text, embedding, source_interaction,
    embedding_model=None, embedding_revision=None, embedding_dim=None,
):
    try:
        _insert_lesson_rows(
            conn, lesson_id, text, embedding, source_interaction,
            embedding_model=embedding_model,
            embedding_revision=embedding_revision,
            embedding_dim=embedding_dim,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


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


_INTERACTION_OUTCOME_EVIDENCE_SQL = (
    "SELECT i.id, substr(COALESCE(i.task,''),1,?) AS task, "
    "length(COALESCE(i.task,'')) AS task_length, "
    "substr(COALESCE(i.response,''),1,?) AS response, "
    "length(COALESCE(i.response,'')) AS response_length, "
    "i.ts AS interaction_ts, i.rowid AS interaction_rowid, "
    "o.signal, o.reward, o.ts AS outcome_ts, o.rowid AS outcome_rowid "
    "FROM interactions AS i NOT INDEXED "
    "JOIN outcomes AS o INDEXED BY idx_outcomes_interaction "
    "ON o.interaction_id=i.id "
    "ORDER BY i.rowid ASC, o.rowid ASC LIMIT ?"
)


def interaction_outcome_evidence(conn, *, limit=200_001, field_limit=32_769):
    """Return stable, append-ordered evidence for training-data selection.

    Export policy belongs outside the storage layer, but it needs every outcome
    (including later failures) and stable row identifiers.  Keeping the query
    here prevents callers from accidentally selecting only positive rows and
    overlooking contradictory evidence.
    """
    limit = int(limit)
    field_limit = int(field_limit)
    if limit < 1 or limit > 1_000_001 or field_limit < 1 or field_limit > 1_000_001:
        raise ValueError("outcome evidence bounds are invalid")
    rows = conn.execute(
        _INTERACTION_OUTCOME_EVIDENCE_SQL,
        (field_limit, field_limit, limit),
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
