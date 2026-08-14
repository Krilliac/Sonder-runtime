"""SQLite-backed memory adapter for the Sonder learning loop. Stdlib only."""
import array
import base64
from dataclasses import dataclass
import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time

import sonder_runtime.adapters.process_liveness as process_liveness
from sonder_runtime.domain.memory import rules as memory_rules


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
    ts TEXT DEFAULT CURRENT_TIMESTAMP,
    -- WHO judged (#62). NOT NULL with deliberately NO DEFAULT: a default is
    -- how the original defect would come back, because a writer that forgot
    -- would keep filing rows under a meaning it never chose. Omitting the
    -- column is a constraint failure, not a silent relabelling. The CHECK
    -- keeps the vocabulary closed so nobody invents 'human'.
    source TEXT NOT NULL CHECK(source IN (
        'caller', 'machine', 'attributed', 'self_curriculum', 'unknown'
    ))
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
    -- Provenance of the credited outcome (#62). Nullable, unlike outcomes.source:
    -- a usage row exists before any outcome lands on it, so NULL means "not yet
    -- credited" as well as "credited before this column existed". Both read as
    -- unknown provenance, which is what the eviction filter treats them as.
    outcome_source TEXT,
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
    -- Why the terminal state came out the way it did ('stored',
    -- 'not_concrete', 'exact_duplicate', 'semantic_duplicate', ...). NULL on
    -- rows written before the column existed and on rows whose finalizer
    -- returned no reason; every reader must tolerate NULL.
    result_reason TEXT,
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
    -- NULL is deliberately the legacy/local-global scope.  A non-null value
    -- is an account boundary supplied by the authenticated serving layer.
    account_scope TEXT,
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
CREATE TABLE IF NOT EXISTS task_deps (
    task_id TEXT NOT NULL,
    depends_on TEXT NOT NULL,
    created_ts TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (task_id, depends_on)
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
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
    created_ts TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_ts TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(scope, key)
);
CREATE TABLE IF NOT EXISTS refinement_history (
    id TEXT PRIMARY KEY,
    target_kind TEXT NOT NULL CHECK(target_kind = 'preference'),
    target_id TEXT NOT NULL,
    operation TEXT NOT NULL CHECK(operation IN ('apply', 'rollback')),
    parent_refinement_id TEXT,
    execution_scope TEXT NOT NULL DEFAULT 'local' CHECK(execution_scope = 'local'),
    expected_version INTEGER NOT NULL CHECK(expected_version >= 1),
    before_version INTEGER NOT NULL CHECK(before_version >= 1),
    after_version INTEGER NOT NULL CHECK(after_version > before_version),
    before_digest TEXT NOT NULL CHECK(length(before_digest) = 64),
    after_digest TEXT NOT NULL CHECK(length(after_digest) = 64),
    before_json TEXT NOT NULL CHECK(length(before_json) <= 16384),
    after_json TEXT NOT NULL CHECK(length(after_json) <= 16384),
    evidence_json TEXT NOT NULL CHECK(length(evidence_json) <= 16384),
    expected_outcome TEXT NOT NULL CHECK(length(expected_outcome) BETWEEN 1 AND 2048),
    created_ts TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TRIGGER IF NOT EXISTS refinement_history_no_update
BEFORE UPDATE ON refinement_history BEGIN
    SELECT RAISE(ABORT, 'refinement history is append-only');
END;
CREATE TRIGGER IF NOT EXISTS refinement_history_no_delete
BEFORE DELETE ON refinement_history BEGIN
    SELECT RAISE(ABORT, 'refinement history is append-only');
END;
"""


def connect(path=":memory:", check_same_thread=True):
    conn = sqlite3.connect(path, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    # busy_timeout must be the FIRST statement executed. Setting journal_mode
    # takes a brief exclusive lock, and init_db opens with BEGIN IMMEDIATE --
    # both contend whenever a second Sonder process is live, which is an
    # ordinary setup: the MCP server and sonder_serve.py share one memory.db
    # exactly as the README describes. Ordered the other way round, those two
    # statements ran under SQLite's default of not waiting at all, so the
    # loser failed instantly with "database is locked" instead of waiting its
    # turn. It surfaced in the desktop app as "Connection closed before full
    # header was received", with the OperationalError only in the server log.
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")
    init_db(conn)
    return conn


def _column_names(conn, table):
    return {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table).fetchall()}


def _good_outcome_signals():
    return tuple(sorted(
        signal
        for signal in memory_rules.VALID_SIGNALS
        if memory_rules.reward_is_good(signal)
    ))


def _migrate_outcomes_source(conn):
    """Give `outcomes` a NOT NULL provenance column, backfilled `unknown`.

    A plain ``ALTER TABLE ... ADD COLUMN`` cannot express this: SQLite demands
    a non-null DEFAULT for a NOT NULL column, and a default is precisely what
    must not exist here -- it would let a future writer keep omitting the value
    and have the store quietly decide what it meant. So the table is rebuilt.

    The backfill is `unknown` for EVERY existing row, and that is a finding,
    not a shortcut. Measured on the live store: 9,450 rows across seven
    signals, and no column, join, or id shape separates a caller judgement from
    a machine verdict or a self-curriculum result -- ``curriculum_run`` and
    ``game_ladder`` call the very same caller-facing ``record_outcome`` tool a
    human does. Labelling them anything else would be inventing the evidence
    the column exists to stop inventing.

    Indexes are intentionally not recreated here: ``init_db`` recreates all of
    them immediately after ``_migrate`` returns, in the same transaction.
    """
    if "source" in _column_names(conn, "outcomes"):
        return
    conn.execute(
        "CREATE TABLE outcomes_with_source ("
        "interaction_id TEXT, signal TEXT, reward REAL, "
        "ts TEXT DEFAULT CURRENT_TIMESTAMP, "
        "source TEXT NOT NULL CHECK(source IN ("
        "'caller', 'machine', 'attributed', 'self_curriculum', 'unknown')))"
    )
    conn.execute(
        "INSERT INTO outcomes_with_source"
        "(rowid, interaction_id, signal, reward, ts, source) "
        "SELECT rowid, interaction_id, signal, reward, ts, ? FROM outcomes",
        (memory_rules.OUTCOME_SOURCE_UNKNOWN,),
    )
    conn.execute("DROP TABLE outcomes")
    conn.execute("ALTER TABLE outcomes_with_source RENAME TO outcomes")


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
                memory_rules.GOOD_THRESHOLD,
                *good_signals,
                memory_rules.GOOD_THRESHOLD,
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
    preference_cols = _column_names(conn, "preferences")
    if "revision" not in preference_cols:
        conn.execute(
            "ALTER TABLE preferences ADD COLUMN revision INTEGER NOT NULL "
            "DEFAULT 1 CHECK(revision >= 1)"
        )
    usage_cols = _column_names(conn, "lesson_usage")
    if "outcome_ts" not in usage_cols:
        conn.execute("ALTER TABLE lesson_usage ADD COLUMN outcome_ts TEXT")
    if "outcome_source" not in usage_cols:
        # Nullable and unbackfilled, for the same reason as outcomes.source:
        # the provenance of a credit written before this column is not
        # recoverable. NULL reads as unknown, and unknown keeps driving the
        # eviction gate exactly as it does today -- see
        # memory_rules.EVICTION_INELIGIBLE_OUTCOME_SOURCES.
        conn.execute("ALTER TABLE lesson_usage ADD COLUMN outcome_source TEXT")
    distillation_cols = _column_names(conn, "lesson_distillations")
    if "result_reason" not in distillation_cols:
        # Nullable and unbackfilled on purpose: the reason for a historical
        # terminal state was never recorded, so any value written here would be
        # invented. NULL is the honest "we did not observe this".
        conn.execute(
            "ALTER TABLE lesson_distillations ADD COLUMN result_reason TEXT"
        )
    # Before the dedupe: the rebuild drops the table and with it the unique
    # index the dedupe uses as its "already done" marker, so running it the
    # other way round would leave the dedupe re-scanning on every open.
    _migrate_outcomes_source(conn)
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
    task_cols = _column_names(conn, "tasks")
    if "account_scope" not in task_cols:
        # Do not invent account ownership for durable legacy tasks.  NULL is
        # the unscoped local/global mode, so old callers retain their exact
        # visibility while account-scoped callers fail closed.
        conn.execute("ALTER TABLE tasks ADD COLUMN account_scope TEXT")
        # The old index cannot serve account-bounded queries.  init_db
        # recreates this named index immediately after migrations finish.
        conn.execute("DROP INDEX IF EXISTS idx_tasks_status_project")


# Bump when _migrate()/post-migration indexes gain a step that _SCHEMA's own
# text does not change.
# Schema-text edits are picked up automatically -- see _schema_stamp().
_MIGRATION_REVISION = 3


def _schema_stamp():
    """A 31-bit stamp identifying this build's schema + migration revision.

    Stored in PRAGMA user_version so a connection can tell, with a pure read
    and no lock, whether the database it just opened is already current.
    Derived from the schema text so editing _SCHEMA invalidates it on its own;
    _MIGRATION_REVISION covers migration-only changes.
    """
    digest = hashlib.sha256(
        (_SCHEMA + "\x00" + str(_MIGRATION_REVISION)).encode("utf-8")
    ).digest()
    return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF


def init_db(conn):
    # Fast path: every connection used to open a write transaction and replay
    # the whole schema plus a dozen CREATE INDEX statements, even when there
    # was nothing to do. _open_db() runs per HTTP request, so a second live
    # Sonder process -- the MCP server alongside sonder_serve.py, the setup
    # the README describes -- meant every request fought for the write lock
    # and lost with "database is locked". busy_timeout alone does not fix
    # that: it only decides how long each request waits before failing.
    # Reading user_version takes no lock at all.
    stamp = _schema_stamp()
    try:
        if conn.execute("PRAGMA user_version").fetchone()[0] == stamp:
            return
    except sqlite3.DatabaseError:
        pass  # unreadable pragma: fall through and do the full init

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
            "CREATE INDEX IF NOT EXISTS idx_interactions_recall_project "
            "ON interactions(project, ts DESC, id DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_interactions_recall_global "
            "ON interactions(ts DESC, id DESC)"
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
            "ON tasks(account_scope, status, project, updated_ts)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_task_events_task "
            "ON task_events(task_id, ts)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_preferences_scope_enabled "
            "ON preferences(scope, enabled, updated_ts)"
        )
        # Stamp last, inside the same transaction, so the fast path is only
        # armed once every statement above has actually landed. A crash
        # part-way through leaves the old stamp and the next connection
        # redoes the work.
        conn.execute("PRAGMA user_version=%d" % stamp)
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
    refusal that wrongly denied web access while web tools were enabled).

    The lessons distilled FROM the interaction go with it. Deleting only the
    ledger row left the lesson itself in `lessons`/`lessons_fts`, where it kept
    being retrieved and injected into later prompts -- the one trace that
    actually reaches the model survived the purge that exists to stop it, and
    `lesson_exists_for_interaction` still answered True for a row that was
    gone. lessons_fts is a plain fts5 table with no delete triggers, so its
    mirror row has to be removed explicitly (see delete_lesson)."""
    distilled = [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM lessons WHERE source_interaction=?",
            (interaction_id,),
        ).fetchall()
    ]
    for lesson_id in distilled:
        _delete_lesson_rows(conn, lesson_id)
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


def _checked_outcome_source(source):
    """Validate provenance at the storage boundary, never infer it.

    Keyword-only and required at every call site above this, so a writer that
    omits it fails at the call rather than being assigned a meaning. The
    database refuses the row as well (NOT NULL, closed CHECK); this raises the
    error that names the problem instead of a bare IntegrityError.
    """
    if not memory_rules.outcome_source_is_valid(source):
        raise ValueError(
            "outcome source must be one of %s (got %r); it records WHICH "
            "writer produced the verdict and is never inferred"
            % (", ".join(sorted(memory_rules.OUTCOME_SOURCES)), source)
        )
    return source


def record_outcome_row(conn, interaction_id, signal, reward_value, *, source):
    """Record one signal once; return whether this call inserted the evidence.

    ``source`` is required and keyword-only: it is a fact about the writing
    path, so no caller may leave it to be guessed later. See
    ``memory_rules.OUTCOME_SOURCES``.
    """
    _checked_outcome_source(source)
    signal = str(signal or "").strip()
    if signal not in memory_rules.VALID_SIGNALS:
        raise ValueError("signal is not a supported grounded outcome")
    if isinstance(reward_value, bool):
        raise ValueError("reward must match the canonical signal reward")
    try:
        reward_value = float(reward_value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("reward must match the canonical signal reward") from exc
    canonical = memory_rules.reward_score(signal)
    if not math.isfinite(reward_value) or reward_value != canonical:
        raise ValueError("reward must match the canonical signal reward")
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO outcomes"
            "(interaction_id, signal, reward, source) VALUES(?, ?, ?, ?)",
            (interaction_id, signal, canonical, source),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return cur.rowcount == 1


def outcome_row_source(conn, interaction_id, signal):
    """Provenance of a stored outcome, or None when no such row exists.

    Exists so a retry path can re-assert an existing row without inventing a
    provenance for it: ``uq_outcomes_interaction_signal_nonnull`` plus
    ``INSERT OR IGNORE`` means the first writer's source is the permanent one.
    """
    row = conn.execute(
        "SELECT source FROM outcomes WHERE interaction_id=? AND signal=? "
        "ORDER BY rowid ASC LIMIT 1",
        (interaction_id, signal),
    ).fetchone()
    return None if row is None else row[0]


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
    # A contradiction is a genuinely NEGATIVE outcome (reward < 0) or a
    # corrupt row (null signal/reward) - real evidence the work was bad or
    # untrustworthy. A weak positive below the good threshold, such as
    # "compiled" (0.70) once GOOD_THRESHOLD rose to 0.71, is neither good
    # enough to ground a lesson nor evidence against one, so it never cancels
    # an otherwise-clean distillation.
    # The provenance filter here is ASYMMETRIC, and that is the point (#62).
    #
    # `has_good` excludes heuristically-`attributed` rows: a lesson is durable
    # material the retriever will serve for months, so the evidence grounding
    # it must be about the work it claims to be about, and that path matches a
    # verification to a generation by project + time window. Without this, a
    # caller recording a merely weak-positive signal (`compiled`, 0.70 -- not
    # good, not a contradiction) could claim a distillation whose ONLY good
    # evidence is a row nobody reviewed and nothing firmly linked.
    #
    # `has_contradiction` deliberately does NOT exclude them. Dropping evidence
    # that the work was bad would lose a real negative to protect a lesson,
    # which is the worse mistake in both directions this module already
    # reasons about: `attributed` may block a lesson, never ground one.
    ineligible = sorted(memory_rules.EVICTION_INELIGIBLE_OUTCOME_SOURCES)
    source_filter = " AND good.source NOT IN (%s)" % ",".join(
        "?" for _ in ineligible
    )
    row = conn.execute(
        "SELECT EXISTS(SELECT 1 FROM outcomes good "
        "WHERE good.interaction_id=? AND good.signal IN (%s) "
        "AND good.reward >= ?%s) AS has_good, "
        "EXISTS(SELECT 1 FROM outcomes bad "
        "WHERE bad.interaction_id=? AND (bad.signal IS NULL "
        "OR bad.reward IS NULL OR bad.reward < 0)) AS has_contradiction"
        % (placeholders, source_filter),
        (
            interaction_id, *good_signals, memory_rules.GOOD_THRESHOLD,
            *ineligible, interaction_id,
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
    conn, interaction_id, signal, reward_value, *, source, claim_token=None,
    owner_pid=None, owner_identity=None, owner_probe=None, now=None,
    claim_distillation=True,
):
    """Atomically record evidence, credit usage, and claim eligible distillation.

    A duplicate non-null interaction/signal is a storage no-op and does not move
    ``lesson_usage.outcome_ts``. It can still reacquire a retryable job or recover
    one whose exact PID/process-start owner is confirmed dead. UNKNOWN liveness
    never steals a claim. Contradictory evidence cancels only live jobs.

    ``source`` is required (#62) and is stamped on both the outcome row and the
    ``lesson_usage`` credit, so the eviction gate can tell the populations apart
    instead of averaging them.

    ``claim_distillation=False`` records the evidence and credits usage but
    neither claims nor cancels a distillation job. It exists for the machine
    attribution path, which has no way to *finish* a claim: claiming there would
    park the interaction's single distillation slot on a worker that never
    returns, and cancelling there would let an unreviewed verdict destroy a
    caller's live job. Recording evidence is safe; deciding a job's fate is not.
    """
    _checked_outcome_source(source)
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
    if signal not in memory_rules.VALID_SIGNALS:
        raise ValueError("signal is not a supported grounded outcome")
    canonical_reward = memory_rules.reward_score(signal)
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
            "INSERT OR IGNORE INTO outcomes"
            "(interaction_id, signal, reward, source) VALUES(?, ?, ?, ?)",
            (interaction_id, signal, reward_value, source),
        )
        outcome_inserted = outcome_cur.rowcount == 1
        usage_rows_updated = 0
        if outcome_inserted:
            usage_cur = conn.execute(
                "UPDATE lesson_usage SET outcome_signal=?, reward=?, "
                "outcome_ts=CURRENT_TIMESTAMP, outcome_source=? "
                "WHERE interaction_id=?",
                (signal, reward_value, source, interaction_id),
            )
            usage_rows_updated = usage_cur.rowcount

        claimed = False
        if claim_distillation:
            has_good, has_contradiction = _distillation_evidence(
                conn, interaction_id,
            )
            if not has_good or has_contradiction:
                _cancel_live_distillation(
                    conn, interaction_id, signal,
                    "contradictory outcome evidence",
                )
            else:
                claimed = _claim_distillation(
                    conn, interaction_id, signal, claim_token, owner,
                    owner_probe, claimed_at,
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


def list_retryable_distillations(conn, limit=32):
    """Oldest-first (interaction_id, signal) pairs whose distillation deferred.

    Deferred jobs are normally reclaimed only when another outcome lands on
    the same interaction; campaign interactions get exactly one outcome, so a
    quiet-time drain uses this listing to retry them explicitly.
    """
    rows = conn.execute(
        "SELECT interaction_id, signal FROM lesson_distillations "
        "WHERE state=? ORDER BY updated_ts ASC, interaction_id ASC LIMIT ?",
        (DISTILLATION_RETRYABLE, max(1, int(limit))),
    ).fetchall()
    return [(str(row[0]), str(row[1] or "")) for row in rows]


def count_retryable_distillations(conn) -> int:
    """How many distillations are deferred in total, ignoring any batch limit.

    ``list_retryable_distillations`` returns a LIMIT-bounded window, so counting
    what stayed deferred within that window answers "how much of this batch
    failed", not "how big is the backlog". A caller that drains 16 of 500 and
    reports the window count prints "still deferred 0" with 484 outstanding.
    """
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM lesson_distillations WHERE state=?",
            (DISTILLATION_RETRYABLE,),
        ).fetchone()[0]
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


# Reasons are short stable names ('semantic_duplicate' is the longest today).
# The cap stops a finalizer that returns free text from turning the ledger into
# a log; anything longer than this is not a reason worth grouping by.
DISTILLATION_REASON_MAX = 64


def normalize_distillation_reason(value):
    """Reduce a finalizer's ``result`` metadata to a groupable reason name.

    Only a non-empty string is a reason. Finalizers are allowed to return
    richer ``result`` payloads (a mapping, or nothing at all), and those carry
    no single value the ledger can group by, so they persist as NULL rather
    than as a stringified dict that no SQL breakdown could ever aggregate.
    """
    if not isinstance(value, str):
        return None
    reason = value.strip()
    if not reason:
        return None
    return reason[:DISTILLATION_REASON_MAX]


def distillation_reason_counts(conn, states=None):
    """Count terminal distillation rows grouped by state and result reason.

    This is the query that answers "where is distillation yield lost?".
    Answering it before the reason was persisted meant replaying historical
    interactions through a live model, because the database held only the
    terminal state. Rows written before the column existed report
    ``reason=None``; that is a real, distinguishable answer ("not recorded"),
    not a bug, and callers must not read it as a rejection reason.
    """
    query = (
        "SELECT state, result_reason AS reason, COUNT(*) AS count "
        "FROM lesson_distillations"
    )
    params = []
    if states:
        wanted = [str(state) for state in states]
        query += " WHERE state IN (%s)" % ",".join("?" for _ in wanted)
        params.extend(wanted)
    query += (
        " GROUP BY state, result_reason "
        "ORDER BY count DESC, state ASC, reason IS NULL, reason ASC"
    )
    return [
        {"state": row["state"], "reason": row["reason"], "count": row["count"]}
        for row in conn.execute(query, params).fetchall()
    ]


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

        # Persisted in the same UPDATE as the terminal state so a reason can
        # never disagree with the state it explains, and so no reader can see
        # a terminal row whose reason has not landed yet.
        result_reason = normalize_distillation_reason(body_result.get("result"))
        cur = conn.execute(
            "UPDATE lesson_distillations SET state=?, result_reason=?, "
            "claim_token=NULL, "
            "owner_pid=NULL, owner_identity=NULL, claimed_at=NULL, last_error=NULL, "
            "updated_ts=CURRENT_TIMESTAMP, completed_ts=CURRENT_TIMESTAMP "
            "WHERE interaction_id=? AND state=? AND claim_token=?",
            (
                terminal_state, result_reason, interaction_id,
                DISTILLATION_CLAIMED, claim_token,
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


def _delete_lesson_rows(conn, lesson_id):
    """Every table a lesson lives in, without committing.

    Shared with delete_interaction so a purge cannot delete a lesson from
    `lessons` and leave it searchable in the FTS mirror.
    """
    cur = conn.execute("DELETE FROM lessons WHERE id=?", (lesson_id,))
    conn.execute("DELETE FROM lessons_fts WHERE lesson_id=?", (lesson_id,))
    conn.execute("DELETE FROM lesson_usage WHERE lesson_id=?", (lesson_id,))
    return cur.rowcount > 0


def delete_lesson(conn, lesson_id):
    """Remove a lesson from both the lessons table and its manual FTS mirror.

    Returns True if a row was deleted. lessons_fts is a plain (non-content) fts5
    table with no delete triggers, so its row must be removed explicitly.
    """
    deleted = _delete_lesson_rows(conn, lesson_id)
    conn.commit()
    return deleted


def log_lesson_usage(conn, lesson_ids, interaction_id, task):
    for lesson_id in lesson_ids or []:
        conn.execute(
            "INSERT OR IGNORE INTO lesson_usage(lesson_id, interaction_id, task) "
            "VALUES(?, ?, ?)",
            (lesson_id, interaction_id, task),
        )
    conn.commit()


def record_lesson_usage_outcome(conn, interaction_id, signal, reward, *, source=None):
    """Credit a retrieval with the outcome it earned, stamped with provenance.

    New callers supply ``source`` for the same reason it is on ``record_outcome_row``:
    this row feeds ``lesson_usage_stats`` -> ``retriever.lesson_quarantine``,
    which removes lessons from retrieval. Evidence with no provenance cannot be
    excluded from that gate later.
    """
    # Keep the legacy four-argument call usable for rows that predate source
    # provenance.  NULL is intentionally preserved as legacy/unknown rather
    # than invented as caller or machine evidence.
    if source is not None:
        _checked_outcome_source(source)
    conn.execute(
        "UPDATE lesson_usage SET outcome_signal=?, reward=?, "
        "outcome_ts=CURRENT_TIMESTAMP, outcome_source=? WHERE interaction_id=?",
        (signal, reward, source, interaction_id),
    )
    conn.commit()


def lesson_usage_history(conn):
    """Ordered outcome evidence for every scored retrieval, one row per use.

    The epoch reducer below and retriever.attributable_losses walk this exact
    sequence. They used to issue the query separately -- byte-identical SQL,
    same rows, same order -- so one retrieval sorted the whole lesson_usage
    table twice before the model was ever called (measured 52 ms per scan over
    11k live rows, on the blocking pre-generation path). Owning the query here
    lets a caller that needs both pay for the scan once, and keeps the two
    epoch definitions from drifting apart.
    """
    excluded = sorted(memory_rules.EVICTION_INELIGIBLE_OUTCOME_SOURCES)
    placeholders = ",".join("?" for _ in excluded)
    # `outcome_source IS NULL` is kept deliberately. Those are the rows written
    # before the column existed, and dropping them would shrink the gate's
    # evidence to nothing on every existing store -- a gate that stops firing
    # because its input vanished, reported as an improvement. Only the one
    # named ineligible source is removed.
    return conn.execute(
        "SELECT lesson_id, interaction_id, task, reward, "
        "COALESCE(outcome_ts, ts) AS evidence_ts "
        "FROM lesson_usage WHERE reward IS NOT NULL "
        "AND (outcome_source IS NULL OR outcome_source NOT IN (%s)) "
        "ORDER BY lesson_id, datetime(evidence_ts), rowid" % placeholders,
        tuple(excluded),
    ).fetchall()


def lesson_usage_stats(conn, history=None):
    """Lifetime counters plus the current evidence epoch, per lesson.

    ``history`` accepts a pre-fetched lesson_usage_history() result so a caller
    that also needs the raw evidence (retriever.usage_stats_with_attribution)
    scans lesson_usage once rather than once per consumer.
    """
    excluded = sorted(memory_rules.EVICTION_INELIGIBLE_OUTCOME_SOURCES)
    placeholders = ",".join("?" for _ in excluded)
    # `uses` still counts every retrieval, credited or not -- that is what the
    # word means and learning_health already documents it. Only the reward
    # aggregates are restricted, because those are what quarantines a lesson.
    rows = conn.execute(
        "SELECT lesson_id, COUNT(*) AS uses, "
        "SUM(CASE WHEN %(elig)s AND reward > 0 THEN 1 ELSE 0 END) AS wins, "
        "SUM(CASE WHEN %(elig)s AND reward < 0 THEN 1 ELSE 0 END) AS losses, "
        "AVG(CASE WHEN %(elig)s AND reward IS NOT NULL THEN reward END) "
        "AS avg_reward, "
        "AVG(CASE WHEN %(elig)s AND (outcome_source = 'caller' OR "
        "(outcome_source IS NULL AND outcome_signal IN "
        "('used','copied','edited','accepted','rejected'))) "
        "AND reward IS NOT NULL THEN reward END) AS avg_reward_caller, "
        "SUM(CASE WHEN %(elig)s AND (outcome_source = 'caller' OR "
        "(outcome_source IS NULL AND outcome_signal IN "
        "('used','copied','edited','accepted','rejected'))) "
        "AND reward IS NOT NULL THEN 1 ELSE 0 END) AS scored_caller, "
        "AVG(CASE WHEN %(elig)s AND (outcome_source != 'caller' OR "
        "(outcome_source IS NULL AND outcome_signal NOT IN "
        "('used','copied','edited','accepted','rejected'))) "
        "AND reward IS NOT NULL THEN reward END) "
        "AS avg_reward_execution, "
        "SUM(CASE WHEN %(elig)s AND (outcome_source != 'caller' OR "
        "(outcome_source IS NULL AND outcome_signal NOT IN "
        "('used','copied','edited','accepted','rejected'))) "
        "AND reward IS NOT NULL THEN 1 ELSE 0 END) AS scored_execution "
        "FROM lesson_usage GROUP BY lesson_id" % {
            "elig": "(outcome_source IS NULL OR outcome_source NOT IN (%s))"
                    % placeholders,
        },
        tuple(excluded) * 7,
    ).fetchall()
    stats = {r["lesson_id"]: dict(r) for r in rows}

    # Keep ordered evidence alongside the lifetime counters. Retrieval policy
    # needs to distinguish a lesson that recovered from one that relapsed after
    # an old success; all-history aggregates cannot express that distinction.
    histories = lesson_usage_history(conn) if history is None else history
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
    """Return token totals with measured/estimated provenance counts.

    Numeric columns alone are not proof that a count came from the provider:
    current callers persist character estimates when Ollama omits usage, and
    historical callers can have unknown provenance.  Keep the total useful,
    while reporting those categories honestly.
    """
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
        "SUM(CASE WHEN lower(COALESCE(token_source, '')) = 'ollama' "
        "OR lower(COALESCE(token_source, '')) LIKE 'ollama+%%' THEN 1 ELSE 0 END) AS exact_rows, "
        "SUM(CASE WHEN token_source IS NULL AND tokens_in IS NULL AND tokens_out IS NULL "
        "OR lower(COALESCE(token_source, '')) = 'estimated' "
        "OR lower(COALESCE(token_source, '')) LIKE 'estimated+%%' THEN 1 ELSE 0 END) AS estimated_rows, "
        "SUM(CASE WHEN lower(COALESCE(token_source, '')) = 'mixed' "
        "OR lower(COALESCE(token_source, '')) LIKE 'mixed+%%' THEN 1 ELSE 0 END) AS mixed_rows, "
        "SUM(CASE WHEN NOT ("
        "lower(COALESCE(token_source, '')) = 'ollama' "
        "OR lower(COALESCE(token_source, '')) LIKE 'ollama+%%' "
        "OR (token_source IS NULL AND tokens_in IS NULL AND tokens_out IS NULL) "
        "OR lower(COALESCE(token_source, '')) = 'estimated' "
        "OR lower(COALESCE(token_source, '')) LIKE 'estimated+%%' "
        "OR lower(COALESCE(token_source, '')) = 'mixed' "
        "OR lower(COALESCE(token_source, '')) LIKE 'mixed+%%') THEN 1 ELSE 0 END) AS unknown_rows, "
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
        "mixed_rows": int(row["mixed_rows"] or 0),
        "unknown_rows": int(row["unknown_rows"] or 0),
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
        "SUM(CASE WHEN lower(COALESCE(token_source, '')) = 'ollama' "
        "OR lower(COALESCE(token_source, '')) LIKE 'ollama+%%' THEN 1 ELSE 0 END) AS exact_rows, "
        "SUM(CASE WHEN token_source IS NULL AND tokens_in IS NULL AND tokens_out IS NULL "
        "OR lower(COALESCE(token_source, '')) = 'estimated' "
        "OR lower(COALESCE(token_source, '')) LIKE 'estimated+%%' THEN 1 ELSE 0 END) AS estimated_rows, "
        "SUM(CASE WHEN lower(COALESCE(token_source, '')) = 'mixed' "
        "OR lower(COALESCE(token_source, '')) LIKE 'mixed+%%' THEN 1 ELSE 0 END) AS mixed_rows, "
        "SUM(CASE WHEN NOT ("
        "lower(COALESCE(token_source, '')) = 'ollama' "
        "OR lower(COALESCE(token_source, '')) LIKE 'ollama+%%' "
        "OR (token_source IS NULL AND tokens_in IS NULL AND tokens_out IS NULL) "
        "OR lower(COALESCE(token_source, '')) = 'estimated' "
        "OR lower(COALESCE(token_source, '')) LIKE 'estimated+%%' "
        "OR lower(COALESCE(token_source, '')) = 'mixed' "
        "OR lower(COALESCE(token_source, '')) LIKE 'mixed+%%') THEN 1 ELSE 0 END) AS unknown_rows, "
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
            "mixed_rows": int(row["mixed_rows"] or 0),
            "unknown_rows": int(row["unknown_rows"] or 0),
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "tokens_total": tokens_in + tokens_out,
        })
    return out


def outcome_signal_counts(conn, sources=None):
    """Rows per signal; ``sources`` restricts to named provenances (#62).

    ``sources=None`` is every row, which is what a raw inventory wants. A
    caller computing a RATE must name its population -- a machine `accepted`
    and a caller `accepted` are the same signal and answer different questions.
    """
    if sources is None:
        rows = conn.execute(
            "SELECT signal, COUNT(*) FROM outcomes GROUP BY signal"
        ).fetchall()
        return {r[0]: r[1] for r in rows}
    wanted = sorted(str(s) for s in sources)
    if not wanted:
        return {}
    placeholders = ",".join("?" for _ in wanted)
    rows = conn.execute(
        "SELECT signal, COUNT(*) FROM outcomes WHERE source IN (%s) "
        "GROUP BY signal" % placeholders,
        tuple(wanted),
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def outcome_source_counts(conn):
    """Rows per provenance. The inventory that says how much is still unknown."""
    rows = conn.execute(
        "SELECT source, COUNT(*) FROM outcomes GROUP BY source"
    ).fetchall()
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
    # `source` is carried but NOT filtered on, deliberately (#62). Export
    # policy belongs to the caller, and this query's whole point is that it
    # must not pre-select: dropping rows by provenance would drop contradictory
    # evidence, which is the failure the docstring below already guards against.
    # Surfacing it lets a policy decide with the fact in hand rather than have
    # this layer decide silently on its behalf.
    "o.signal, o.reward, o.source AS outcome_source, "
    "o.ts AS outcome_ts, o.rowid AS outcome_rowid "
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


def _normalize_task_status_filter(status):
    """Parse the task-list status filter, including documented ``a|b`` sets."""
    values = []
    for item in str(status or "").split("|"):
        item = item.strip()
        if item:
            normalized = _normalize_task_status(item)
            if normalized not in values:
                values.append(normalized)
    return values


def _normalize_priority(priority):
    try:
        value = int(priority)
    except (TypeError, ValueError):
        value = 2
    return max(0, min(5, value))


def _normalize_task_account_scope(account_scope):
    """Return the explicit account boundary, or ``None`` for legacy mode."""
    if account_scope is None:
        return None
    if not isinstance(account_scope, str):
        raise ValueError("task account scope must be a string")
    value = account_scope.strip()
    if not value:
        raise ValueError("task account scope must not be empty")
    if len(value) > 256:
        raise ValueError("task account scope is too long")
    return value


def _task_scope_predicate(account_scope):
    """Return a SQL predicate/parameters pair for an optional account scope."""
    scope = _normalize_task_account_scope(account_scope)
    return (("account_scope=?", [scope]) if scope is not None else ("", []))


def log_task_event(conn, task_id, event, note=""):
    conn.execute(
        "INSERT INTO task_events(id, task_id, event, note) VALUES(?, ?, ?, ?)",
        (new_id(), task_id, event, note or ""),
    )
    conn.commit()


def create_task(conn, title, detail="", status="pending", priority=2,
                project="", owner="", parent_id="", task_id=None,
                account_scope=None):
    title = (title or "").strip()
    if not title:
        raise ValueError("task title is required")
    task_id = task_id or new_id()
    normalized = _normalize_task_status(status)
    scope = _normalize_task_account_scope(account_scope)
    parent_id = parent_id or ""
    if scope is not None and parent_id:
        resolved_parent = resolve_task_id(conn, parent_id, account_scope=scope)
        if not resolved_parent:
            raise ValueError("no unique task '%s'" % parent_id)
        parent_id = resolved_parent
    conn.execute(
        "INSERT INTO tasks("
        "id, title, detail, status, priority, project, owner, parent_id, account_scope"
        ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            task_id,
            title,
            detail or "",
            normalized,
            _normalize_priority(priority),
            project or "",
            owner or "",
            parent_id,
            scope,
        ),
    )
    conn.commit()
    log_task_event(conn, task_id, "created", title)
    return get_task(conn, task_id, account_scope=scope)


def resolve_task_id(conn, task_id, account_scope=None):
    value = (task_id or "").strip()
    if not value:
        return None
    scope_predicate, scope_values = _task_scope_predicate(account_scope)
    where_scope = (" AND " + scope_predicate) if scope_predicate else ""
    row = conn.execute(
        "SELECT id FROM tasks WHERE id=?%s" % where_scope,
        tuple([value] + scope_values),
    ).fetchone()
    if row:
        return row["id"]
    rows = conn.execute(
        "SELECT id FROM tasks WHERE id LIKE ?%s "
        "ORDER BY updated_ts DESC, rowid DESC LIMIT 2" % where_scope,
        tuple([value + "%"] + scope_values),
    ).fetchall()
    if len(rows) == 1:
        return rows[0]["id"]
    return None


def get_task(conn, task_id, account_scope=None):
    resolved = resolve_task_id(conn, task_id, account_scope=account_scope) or task_id
    scope_predicate, scope_values = _task_scope_predicate(account_scope)
    where_scope = (" AND " + scope_predicate) if scope_predicate else ""
    row = conn.execute(
        "SELECT * FROM tasks WHERE id=?%s" % where_scope,
        tuple([resolved] + scope_values),
    ).fetchone()
    return dict(row) if row else None


def update_task(conn, task_id, status=None, title=None, detail=None,
                priority=None, project=None, owner=None, note="",
                account_scope=None):
    scope = _normalize_task_account_scope(account_scope)
    resolved = resolve_task_id(conn, task_id, account_scope=scope)
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
        return get_task(conn, resolved, account_scope=scope)
    fields.append("updated_ts=CURRENT_TIMESTAMP")
    values.append(resolved)
    scope_predicate, scope_values = _task_scope_predicate(scope)
    where_scope = (" AND " + scope_predicate) if scope_predicate else ""
    conn.execute(
        "UPDATE tasks SET %s WHERE id=?%s" % (", ".join(fields), where_scope),
        tuple(values + scope_values),
    )
    conn.commit()
    log_task_event(conn, resolved, "updated", note or ", ".join(event_bits))
    return get_task(conn, resolved, account_scope=scope)


def list_tasks(conn, status="", project="", owner="", limit=50, include_done=False,
               account_scope=None):
    limit = max(1, min(int(limit or 50), 200))
    clauses = []
    values = []
    if status:
        statuses = _normalize_task_status_filter(status)
        clauses.append("status IN (%s)" % ", ".join("?" for _ in statuses))
        values.extend(statuses)
    elif not include_done:
        clauses.append("status NOT IN ('done', 'canceled')")
    if project:
        clauses.append("project=?")
        values.append(project)
    if owner:
        clauses.append("owner=?")
        values.append(owner)
    scope_predicate, scope_values = _task_scope_predicate(account_scope)
    if scope_predicate:
        clauses.append(scope_predicate)
        values.extend(scope_values)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        "SELECT * FROM tasks%s ORDER BY priority ASC, updated_ts DESC, rowid DESC LIMIT ?"
        % where,
        tuple(values + [limit]),
    ).fetchall()
    return [dict(r) for r in rows]


def latest_checklist(conn, account_scope=None):
    """Return the most recently updated checklist parent in one scope."""
    scope = _normalize_task_account_scope(account_scope)
    clauses = ["parent.parent_id=''", (
        "EXISTS (SELECT 1 FROM tasks AS child WHERE child.parent_id=parent.id)"
    )]
    values = []
    if scope is not None:
        clauses.append("parent.account_scope=?")
        values.append(scope)
    row = conn.execute(
        "SELECT parent.* FROM tasks AS parent WHERE %s "
        "ORDER BY parent.updated_ts DESC, parent.rowid DESC LIMIT 1"
        % " AND ".join(clauses),
        tuple(values),
    ).fetchone()
    return dict(row) if row else None


def task_events(conn, task_id, limit=20, account_scope=None):
    resolved = resolve_task_id(conn, task_id, account_scope=account_scope)
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

RECALL_CANDIDATE_ROW_LIMIT = 512
RECALL_CANDIDATE_BYTE_LIMIT = 8 * 1024 * 1024
RECALL_CANDIDATE_TIME_LIMIT_S = 0.5
RECALL_MAX_STORED_TASK_CHARS = 64_000
RECALL_RESPONSE_PREFIX_CHARS = 401
RECALL_MAX_EMBEDDING_BYTES = 64 * 1024
RECALL_CURSOR_MAX_CHARS = 6144


@dataclass(frozen=True)
class InteractionCandidatePage:
    """One bounded, newest-first keyset page for semantic scoring."""

    rows: tuple[dict, ...]
    incomplete: bool
    next_cursor: str | None
    termination: str
    rows_examined: int
    bytes_loaded: int


def _recall_row_bytes(row):
    total = 0
    for key in (
        "id", "task", "response", "session_id", "task_embedding_model",
        "task_embedding_revision", "project",
    ):
        value = row[key]
        if isinstance(value, str):
            total += len(value.encode("utf-8", errors="replace"))
        elif isinstance(value, (bytes, bytearray, memoryview)):
            total += len(value)
    embedding = row["task_embedding"]
    if isinstance(embedding, (bytes, bytearray, memoryview)):
        total += len(embedding)
    return total


def _decode_recall_candidate(row):
    decoded = dict(row)
    for key in (
        "id", "task", "response", "session_id", "task_embedding_model",
        "task_embedding_revision", "project",
    ):
        value = decoded.get(key)
        if value is None:
            continue
        if not isinstance(value, (bytes, bytearray, memoryview)):
            return None
        try:
            decoded[key] = bytes(value).decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return None
    return decoded


def _encode_recall_cursor(timestamp, interaction_id):
    payload = json.dumps(
        [timestamp, interaction_id], ensure_ascii=True, separators=(",", ":"),
    ).encode("ascii")
    token = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return "r1." + token


def _decode_recall_cursor(value):
    if not isinstance(value, str) or not value.startswith("r1."):
        raise ValueError("recall candidate cursor is invalid")
    token = value[3:]
    if not token or len(token) > RECALL_CURSOR_MAX_CHARS:
        raise ValueError("recall candidate cursor is invalid")
    try:
        padded = token + "=" * (-len(token) % 4)
        payload = base64.b64decode(
            padded.encode("ascii"), altchars=b"-_", validate=True,
        )
        decoded = json.loads(payload.decode("ascii"))
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("recall candidate cursor is invalid") from exc
    if (
        not isinstance(decoded, list)
        or len(decoded) != 2
        or not all(isinstance(item, str) for item in decoded)
        or not decoded[0]
        or not decoded[1]
        or len(decoded[0]) > 64
        or len(decoded[1]) > 256
        or _encode_recall_cursor(decoded[0], decoded[1]) != value
    ):
        raise ValueError("recall candidate cursor is invalid")
    return decoded[0], decoded[1]


def good_interaction_candidate_page(
    conn, exclude_session=None, project=None, include_all_projects=False,
    *, embedding_model=None, embedding_revision=None, embedding_dim=None,
    cursor=None, row_limit=RECALL_CANDIDATE_ROW_LIMIT,
    byte_limit=RECALL_CANDIDATE_BYTE_LIMIT,
    time_limit_s=RECALL_CANDIDATE_TIME_LIMIT_S, cancel_check=None,
):
    """Return a bounded deterministic recall-candidate page.

    The quality policy is a newest-first window: recent successful solutions
    are most likely to match the current code, model revision, and operational
    constraints. Older windows remain reachable with the exclusive opaque
    timestamp/id cursor. Project/session/outcome/embedding-space filters run in SQLite
    *before* the window, so unrelated rows cannot consume its budget.

    ``incomplete`` is explicit whenever rows, decoded bytes, or elapsed time
    stop enumeration. The query uses recall/project and outcome indexes; no
    content or embedding backfill is required.
    """
    include_all_projects = include_all_projects is True
    if isinstance(row_limit, bool) or not isinstance(row_limit, int):
        raise ValueError("recall candidate row limit must be an integer")
    if isinstance(byte_limit, bool) or not isinstance(byte_limit, int):
        raise ValueError("recall candidate byte limit must be an integer")
    if isinstance(time_limit_s, bool) or not isinstance(time_limit_s, (int, float)):
        raise ValueError("recall candidate time limit must be numeric")
    row_limit = max(1, min(RECALL_CANDIDATE_ROW_LIMIT, row_limit))
    byte_limit = max(1, min(RECALL_CANDIDATE_BYTE_LIMIT, byte_limit))
    time_limit_s = max(0.0, min(RECALL_CANDIDATE_TIME_LIMIT_S, float(time_limit_s)))
    cursor_boundary = _decode_recall_cursor(cursor) if cursor is not None else None

    good_signals = tuple(sorted(
        signal
        for signal in memory_rules.VALID_SIGNALS
        if memory_rules.reward_is_good(signal)
    ))
    placeholders = ",".join("?" for _ in good_signals)
    reward_case = " ".join("WHEN ? THEN ?" for _ in good_signals)
    canonical_rewards = tuple(
        value
        for signal in good_signals
        for value in (signal, memory_rules.reward_score(signal))
    )
    sql = (
        "SELECT CAST(i.ts AS BLOB) AS candidate_ts, "
        "CAST(i.id AS BLOB) AS candidate_cursor_id, "
        "CAST(i.id AS BLOB) AS id, "
        "CAST(i.task AS BLOB) AS task, "
        "CASE WHEN i.response IS NULL THEN NULL "
        "ELSE CAST(substr(i.response,1,?) AS BLOB) END AS response, "
        "i.task_embedding, CAST(i.session_id AS BLOB) AS session_id, "
        "CAST(i.task_embedding_model AS BLOB) AS task_embedding_model, "
        "CAST(i.task_embedding_revision AS BLOB) AS task_embedding_revision, "
        "i.task_embedding_dim, CASE WHEN NULLIF(i.project,'') IS NULL "
        "THEN NULL ELSE CAST(i.project AS BLOB) END AS project "
        "FROM interactions i WHERE i.task_embedding IS NOT NULL "
        "AND typeof(i.id)='text' AND length(i.id) BETWEEN 1 AND 256 "
        "AND typeof(i.ts)='text' AND length(i.ts) BETWEEN 1 AND 64 "
        "AND typeof(i.task)='text' AND length(i.task)<=? "
        "AND (i.response IS NULL OR typeof(i.response)='text') "
        "AND (i.session_id IS NULL OR "
        "(typeof(i.session_id)='text' AND length(i.session_id)<=256)) "
        "AND (i.project IS NULL OR "
        "(typeof(i.project)='text' AND length(i.project)<=4096)) "
        "AND (i.task_embedding_model IS NULL OR "
        "(typeof(i.task_embedding_model)='text' "
        "AND length(i.task_embedding_model)<=256)) "
        "AND (i.task_embedding_revision IS NULL OR "
        "(typeof(i.task_embedding_revision)='text' "
        "AND length(i.task_embedding_revision)<=256)) "
        "AND typeof(i.task_embedding)='blob' "
        "AND length(i.task_embedding) BETWEEN 4 AND ? "
        "AND typeof(i.task_embedding_dim)='integer' "
        "AND i.task_embedding_dim>0 "
        "AND length(i.task_embedding)=i.task_embedding_dim*4 "
        "AND EXISTS (SELECT 1 FROM outcomes good "
        "WHERE good.interaction_id=i.id AND good.signal IN (%s) "
        "AND typeof(good.reward) IN ('integer','real') "
        "AND good.reward=CASE good.signal %s END) "
        "AND NOT EXISTS (SELECT 1 FROM outcomes bad "
        "WHERE bad.interaction_id=i.id AND "
        "(bad.signal NOT IN (%s) OR bad.signal IS NULL "
        "OR typeof(bad.reward) NOT IN ('integer','real') "
        "OR bad.reward!=CASE bad.signal %s END OR bad.reward<?))"
        % (placeholders, reward_case, placeholders, reward_case)
    )
    params = [
        RECALL_RESPONSE_PREFIX_CHARS,
        RECALL_MAX_STORED_TASK_CHARS,
        RECALL_MAX_EMBEDDING_BYTES,
        *good_signals,
        *canonical_rewards,
        *good_signals,
        *canonical_rewards,
        memory_rules.GOOD_THRESHOLD,
    ]
    if exclude_session:
        sql += " AND (i.session_id IS NULL OR i.session_id != ?)"
        params.append(exclude_session)
    if not include_all_projects:
        if project is None:
            sql += (
                " AND NULLIF(i.project,'') IS NULL "
                "AND (i.project_explicit=1 OR i.session_id IS NULL)"
            )
        else:
            if project == "":
                sql += " AND 0"
            else:
                sql += " AND i.project=?"
                params.append(project)
    if embedding_model:
        sql += " AND i.task_embedding_model=?"
        params.append(embedding_model)
    if embedding_revision is not None:
        sql += " AND NULLIF(i.task_embedding_revision,'') IS ?"
        params.append(embedding_revision or None)
    if embedding_dim is not None:
        sql += " AND i.task_embedding_dim=?"
        params.append(embedding_dim)
    if cursor_boundary is not None:
        sql += " AND (i.ts<? OR (i.ts=? AND i.id<?))"
        params.extend((
            cursor_boundary[0], cursor_boundary[0], cursor_boundary[1],
        ))
    sql += " ORDER BY i.ts DESC, i.id DESC LIMIT ?"
    params.append(row_limit + 1)

    deadline = time.monotonic() + time_limit_s
    timed_out = False
    cancelled = False

    def _deadline_reached():
        nonlocal cancelled
        if cancel_check is not None:
            try:
                cancelled = cancel_check() is True
            except Exception:
                cancelled = True
            if cancelled:
                return 1
        return 1 if time.monotonic() >= deadline else 0

    rows = []
    loaded_bytes = 0
    examined = 0
    last_cursor = cursor
    termination = "complete"
    previous_busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    request_busy_timeout = max(1, int(time_limit_s * 1000))
    conn.execute("PRAGMA busy_timeout=%d" % min(
        int(previous_busy_timeout), request_busy_timeout,
    ))
    conn.set_progress_handler(_deadline_reached, 1000)
    try:
        query_cursor = conn.execute(sql, tuple(params))
        while True:
            if _deadline_reached():
                if cancelled:
                    termination = "cancelled"
                else:
                    timed_out = True
                    termination = "time_limit"
                break
            row = query_cursor.fetchone()
            if row is None:
                break
            raw_candidate = dict(row)
            try:
                candidate_ts = bytes(raw_candidate.pop("candidate_ts")).decode(
                    "utf-8", errors="strict",
                )
                candidate_cursor_id = bytes(
                    raw_candidate.pop("candidate_cursor_id")
                ).decode("utf-8", errors="strict")
                row_cursor = _encode_recall_cursor(
                    candidate_ts, candidate_cursor_id,
                )
            except (TypeError, UnicodeDecodeError):
                candidate_ts = None
                row_cursor = last_cursor
            if examined >= row_limit:
                termination = "row_limit"
                break
            row_bytes = _recall_row_bytes(raw_candidate)
            if loaded_bytes + row_bytes > byte_limit:
                termination = "byte_limit"
                if loaded_bytes == 0:
                    # This row can never fit this request budget. Advance past
                    # it so pagination cannot loop forever on malformed data.
                    examined += 1
                    last_cursor = row_cursor
                break
            loaded_bytes += row_bytes
            examined += 1
            last_cursor = row_cursor
            if candidate_ts is None:
                continue
            candidate = _decode_recall_candidate(raw_candidate)
            if candidate is None:
                continue
            rows.append(candidate)
    except sqlite3.OperationalError as exc:
        normalized_error = str(exc).lower()
        if "interrupted" not in normalized_error and "locked" not in normalized_error:
            raise
        if cancelled:
            termination = "cancelled"
        else:
            timed_out = True
            termination = "time_limit"
    finally:
        conn.set_progress_handler(None, 0)
        conn.execute("PRAGMA busy_timeout=%d" % int(previous_busy_timeout))

    incomplete = timed_out or cancelled or termination != "complete"
    # A corrupt ordering key cannot be represented by the public cursor.  Do
    # not hand the caller its input cursor again: that would make a normal
    # ``while page.next_cursor`` consumer retry the same page forever.  The
    # incomplete flag and termination still truthfully report the exhausted
    # request budget, while ``None`` says that this page has no safe resume
    # boundary.
    resume_cursor = last_cursor if incomplete and last_cursor != cursor else None
    return InteractionCandidatePage(
        rows=tuple(rows),
        incomplete=incomplete,
        next_cursor=resume_cursor,
        termination=termination,
        rows_examined=examined,
        bytes_loaded=loaded_bytes,
    )

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
    return list(good_interaction_candidate_page(
        conn,
        exclude_session,
        project=project,
        include_all_projects=include_all_projects,
    ).rows)


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
        "revision=preferences.revision + 1, "
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
        "evidence_count, enabled, revision, created_ts, updated_ts "
        "FROM preferences WHERE %s "
        "ORDER BY confidence DESC, evidence_count DESC, updated_ts DESC LIMIT ?"
        % where,
        tuple(params + [int(limit)]),
    ).fetchall()
    return [dict(r) for r in rows]


def task_children(conn, task_id, account_scope=None):
    scope = _normalize_task_account_scope(account_scope)
    resolved = resolve_task_id(conn, task_id, account_scope=scope)
    if not resolved:
        return []
    scope_predicate, scope_values = _task_scope_predicate(scope)
    where_scope = (" AND " + scope_predicate) if scope_predicate else ""
    rows = conn.execute(
        "SELECT * FROM tasks WHERE parent_id=?%s ORDER BY rowid ASC" % where_scope,
        tuple([resolved] + scope_values),
    ).fetchall()
    return [dict(row) for row in rows]


def delete_task(conn, task_id, account_scope=None):
    scope = _normalize_task_account_scope(account_scope)
    resolved = resolve_task_id(conn, task_id, account_scope=scope)
    if not resolved:
        raise ValueError("no unique task '%s'" % task_id)
    scope_predicate, scope_values = _task_scope_predicate(scope)
    where_scope = (" AND " + scope_predicate) if scope_predicate else ""
    children = conn.execute(
        "SELECT id FROM tasks WHERE parent_id=?%s" % where_scope,
        tuple([resolved] + scope_values),
    ).fetchall()
    child_ids = [r["id"] for r in children]
    all_ids = [resolved] + child_ids
    placeholders = ",".join("?" * len(all_ids))
    conn.execute("DELETE FROM task_events WHERE task_id IN (%s)" % placeholders, all_ids)
    conn.execute("DELETE FROM task_deps WHERE task_id IN (%s) OR depends_on IN (%s)"
                 % (placeholders, placeholders), all_ids + all_ids)
    conn.execute("DELETE FROM tasks WHERE id IN (%s)" % placeholders, all_ids)
    conn.commit()
    log_task_event(conn, resolved, "deleted", "task and %d children removed" % len(child_ids))
    return {"deleted": resolved, "children_removed": len(child_ids)}


def add_task_dep(conn, task_id, depends_on, account_scope=None):
    scope = _normalize_task_account_scope(account_scope)
    resolved = resolve_task_id(conn, task_id, account_scope=scope)
    dep_resolved = resolve_task_id(conn, depends_on, account_scope=scope)
    if not resolved:
        raise ValueError("no unique task '%s'" % task_id)
    if not dep_resolved:
        raise ValueError("no unique task '%s'" % depends_on)
    if resolved == dep_resolved:
        raise ValueError("a task cannot depend on itself")
    conn.execute(
        "INSERT OR IGNORE INTO task_deps(task_id, depends_on) VALUES(?, ?)",
        (resolved, dep_resolved),
    )
    conn.commit()
    log_task_event(conn, resolved, "dep_added", "depends on %s" % dep_resolved[:8])
    return {"task_id": resolved, "depends_on": dep_resolved}


def remove_task_dep(conn, task_id, depends_on, account_scope=None):
    scope = _normalize_task_account_scope(account_scope)
    resolved = resolve_task_id(conn, task_id, account_scope=scope)
    dep_resolved = resolve_task_id(conn, depends_on, account_scope=scope)
    if not resolved or not dep_resolved:
        return {"removed": False}
    conn.execute(
        "DELETE FROM task_deps WHERE task_id=? AND depends_on=?",
        (resolved, dep_resolved),
    )
    conn.commit()
    return {"removed": True, "task_id": resolved, "depends_on": dep_resolved}


def task_dependencies(conn, task_id, account_scope=None):
    scope = _normalize_task_account_scope(account_scope)
    resolved = resolve_task_id(conn, task_id, account_scope=scope)
    if not resolved:
        return []
    scope_predicate, scope_values = _task_scope_predicate(scope)
    where_scope = (" AND t." + scope_predicate) if scope_predicate else ""
    rows = conn.execute(
        "SELECT t.* FROM tasks t JOIN task_deps d ON t.id=d.depends_on "
        "WHERE d.task_id=?%s ORDER BY t.priority ASC, t.rowid ASC" % where_scope,
        tuple([resolved] + scope_values),
    ).fetchall()
    return [dict(r) for r in rows]


def task_dependents(conn, task_id, account_scope=None):
    scope = _normalize_task_account_scope(account_scope)
    resolved = resolve_task_id(conn, task_id, account_scope=scope)
    if not resolved:
        return []
    scope_predicate, scope_values = _task_scope_predicate(scope)
    where_scope = (" AND t." + scope_predicate) if scope_predicate else ""
    rows = conn.execute(
        "SELECT t.* FROM tasks t JOIN task_deps d ON t.id=d.task_id "
        "WHERE d.depends_on=?%s ORDER BY t.priority ASC, t.rowid ASC" % where_scope,
        tuple([resolved] + scope_values),
    ).fetchall()
    return [dict(r) for r in rows]


def task_progress(conn, project="", account_scope=None):
    clauses = []
    values = []
    if project:
        clauses.append("project=?")
        values.append(project)
    scope_predicate, scope_values = _task_scope_predicate(account_scope)
    if scope_predicate:
        clauses.append(scope_predicate)
        values.extend(scope_values)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        "SELECT status, COUNT(*) as cnt FROM tasks%s GROUP BY status" % where,
        tuple(values),
    ).fetchall()
    counts = {r["status"]: r["cnt"] for r in rows}
    total = sum(counts.values())
    done = counts.get("done", 0) + counts.get("canceled", 0)
    return {
        "total": total,
        "pending": counts.get("pending", 0),
        "in_progress": counts.get("in_progress", 0),
        "blocked": counts.get("blocked", 0),
        "done": counts.get("done", 0),
        "canceled": counts.get("canceled", 0),
        "progress_pct": round(100 * done / total, 1) if total else 0.0,
    }


def all_preferences(conn, limit=50, include_disabled=False):
    where = "" if include_disabled else "WHERE enabled=1"
    rows = conn.execute(
        "SELECT id, scope, key, text, source_interaction, confidence, "
        "evidence_count, enabled, revision, created_ts, updated_ts "
        "FROM preferences %s "
        "ORDER BY scope ASC, confidence DESC, evidence_count DESC, updated_ts DESC LIMIT ?"
        % where,
        (int(limit),),
    ).fetchall()
    return [dict(r) for r in rows]


def set_preference_enabled(conn, pref_id_or_key, enabled, scope="global"):
    scope = (scope or "global").strip() or "global"
    cur = conn.execute(
        "UPDATE preferences SET enabled=?, revision=revision + 1, "
        "updated_ts=CURRENT_TIMESTAMP "
        "WHERE id=? OR (scope=? AND key=?)",
        (1 if enabled else 0, pref_id_or_key, scope, pref_id_or_key),
    )
    conn.commit()
    return cur.rowcount
