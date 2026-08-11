"""Record WHO judged: outcomes.source + lesson_usage.outcome_source (#62).

``outcomes`` was ``(interaction_id, signal, reward, ts)``. With no provenance a
machine verdict from ``grounded_outcomes.attribute`` was indistinguishable from
a human pressing "accepted" -- permanently, for every row already written. That
absence cost three separate answers: a decontamination lane could only prove a
bad path was empty because the path happened to be new, an inversion lane could
not count how many of 9,450 rows filed a failing verification as a +1.0 pass,
and a ready fix stayed blocked because crediting ``lesson_usage`` from the
machine path would have let unreviewed verdicts drive live lesson eviction.

The work is done by ``memory_store._migrate``, which owns memory.db's schema and
is idempotent; this module is the ledgered, checksummed record that it happened,
plus a ``verify`` that fails the migration if the column is absent or nullable.

**Backfill: every pre-existing row becomes ``unknown``, and that is the finding.**
Measured read-only against the live store before writing this: 9,450 rows over
seven signals, joined to ``interactions`` for tier, and nothing separates the
populations. ``curriculum_run`` and ``game_ladder`` called the same caller-facing
``record_outcome`` tool a human does, so even the 9,049 ``tests_passed`` rows
cannot be attributed by shape. A confident label would be permanently worse than
an honest blank: the whole defect is that nobody could tell the populations
apart, and inventing the answer removes the last chance to notice.
"""

manages_own_transaction = True


def _db_path(conn) -> str:
    for _, name, filename in conn.execute("PRAGMA database_list"):
        if name == "main":
            return filename
    raise RuntimeError("cannot resolve database path for outcomes.source")


def apply(conn) -> None:
    import memory_store

    path = _db_path(conn)
    legacy = memory_store.connect(path, check_same_thread=False)
    try:
        memory_store.init_db(legacy)
    finally:
        legacy.close()


def verify(conn) -> None:
    columns = {row[1]: row for row in conn.execute("PRAGMA table_info(outcomes)")}
    if "source" not in columns:
        raise RuntimeError("outcomes.source was not created")
    # notnull is column 3 of PRAGMA table_info; dflt_value is column 4.
    if not columns["source"][3]:
        raise RuntimeError("outcomes.source must be NOT NULL")
    if columns["source"][4] is not None:
        raise RuntimeError(
            "outcomes.source must have no DEFAULT: a default is how a writer "
            "that forgets provenance keeps silently filing rows"
        )
    if "outcome_source" not in {
        row[1] for row in conn.execute("PRAGMA table_info(lesson_usage)")
    }:
        raise RuntimeError("lesson_usage.outcome_source was not created")
    unlabelled = conn.execute(
        "SELECT COUNT(*) FROM outcomes WHERE source IS NULL OR source=''"
    ).fetchone()[0]
    if unlabelled:
        raise RuntimeError(
            "%d outcome rows carry no provenance after migration" % unlabelled
        )
