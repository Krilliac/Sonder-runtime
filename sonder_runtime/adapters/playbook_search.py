"""Write-time lexical candidate retrieval using the existing lesson FTS path."""
from sonder_runtime.adapters.memory_store import fts_search
from sonder_runtime.adapters.persistence.owned_sqlite import connect


def duplicate_candidates(query, entries):
    """Select at most 128 candidates without embeddings, models or disk writes.

    Exact duplicate detection still examines all entries before this helper.
    FTS is a candidate generator, not a claim that two notes are equivalent.
    """
    bounded = list(entries)[:512]
    connection = connect(":memory:")
    try:
        connection.execute("CREATE VIRTUAL TABLE lessons_fts USING fts5(lesson_id UNINDEXED, text)")
        connection.executemany(
            "INSERT INTO lessons_fts(lesson_id,text) VALUES(?,?)",
            [(str(index), " ".join(str(row.get(key) or "") for key in ("title", "body", "evidence")))
             for index, row in enumerate(bounded)],
        )
        return [bounded[int(index)] for index in fts_search(connection, query, limit=128)]
    finally:
        connection.close()
