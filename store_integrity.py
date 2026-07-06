"""Lesson-store integrity checker. Stdlib only.

Checks a memory_store connection for corruption that can silently degrade
retrieval and the solver's self-repair loop, without either of them ever
raising an obvious error:

  - orphan_fts:    lessons_fts rows whose lesson_id has no matching row in
                    lessons -- dangling mirror entries. lessons_fts is a
                    plain (non-content) fts5 table with no delete triggers
                    (see memory_store.delete_lesson's docstring), so any
                    deletion path that misses the explicit
                    "DELETE FROM lessons_fts" leaves one of these behind.
  - missing_fts:   lessons rows with no matching lessons_fts row -- the
                    lesson exists and can be recalled by id/embedding, but
                    memory_store.fts_search can never surface it because
                    add_lesson is the only path that populates the mirror
                    and something inserted into lessons without going
                    through it.
  - empty_text:    lessons.text is NULL, '', or whitespace-only -- nothing
                    for FTS or a human to match against.
  - bad_embedding: lessons.embedding is present (NOT NULL) but does not
                    decode as a float32 vector via embeddings.from_blob --
                    would raise mid-retrieval (e.g. inside retriever's
                    cosine ranking) instead of failing here, cheaply and
                    legibly, before it ever reaches that hot path.

Read-only: this module never mutates the store. It only reads and reports;
`memory_store.delete_lesson` / `memory_store.add_lesson` remain the only
mutation paths for lessons/lessons_fts, and this module doesn't call them.
"""
import argparse
import collections

import embeddings as _embeddings
import memory_store

Issue = collections.namedtuple("Issue", ["code", "lesson_id", "detail"])


def _lesson_ids(conn):
    return {r[0] for r in conn.execute("SELECT id FROM lessons").fetchall()}


def _fts_lesson_ids(conn):
    return {r[0] for r in conn.execute("SELECT lesson_id FROM lessons_fts").fetchall()}


def check_orphan_fts(conn):
    """lessons_fts rows whose lesson_id has no matching lessons.id."""
    lesson_ids = _lesson_ids(conn)
    fts_ids = _fts_lesson_ids(conn)
    return [
        Issue("orphan_fts", lid, "lessons_fts row has no matching lessons.id")
        for lid in sorted(fts_ids - lesson_ids)
    ]


def check_missing_fts(conn):
    """lessons rows with no matching lessons_fts row (unsearchable by fts_search)."""
    lesson_ids = _lesson_ids(conn)
    fts_ids = _fts_lesson_ids(conn)
    return [
        Issue("missing_fts", lid, "lesson has no lessons_fts mirror row")
        for lid in sorted(lesson_ids - fts_ids)
    ]


def check_empty_text(conn):
    """lessons whose text is NULL, empty, or whitespace-only."""
    rows = conn.execute("SELECT id, text FROM lessons").fetchall()
    return [
        Issue("empty_text", r[0], "lessons.text is empty/whitespace-only")
        for r in rows
        if not (r[1] or "").strip()
    ]


def check_bad_embeddings(conn, decode_fn=_embeddings.from_blob):
    """lessons with a non-NULL embedding blob that fails to decode.

    decode_fn is injectable (bytes -> list[float], raising on malformed
    input) so tests never need a real embedding model -- defaults to
    embeddings.from_blob, which is pure array decoding with no network/GPU
    dependency of its own.
    """
    rows = conn.execute(
        "SELECT id, embedding FROM lessons WHERE embedding IS NOT NULL"
    ).fetchall()
    issues = []
    for lid, blob in rows:
        try:
            vec = decode_fn(blob)
        except Exception as exc:  # any decode failure is a reportable issue, not a crash
            issues.append(
                Issue("bad_embedding", lid, "embedding failed to decode: %r" % (exc,))
            )
            continue
        if not vec:
            issues.append(Issue("bad_embedding", lid, "embedding decoded to an empty vector"))
    return issues


def check_store(conn, decode_fn=_embeddings.from_blob):
    """Run every check against conn. Returns (ok, issues) -- issues: list[Issue]."""
    issues = []
    issues.extend(check_orphan_fts(conn))
    issues.extend(check_missing_fts(conn))
    issues.extend(check_empty_text(conn))
    issues.extend(check_bad_embeddings(conn, decode_fn=decode_fn))
    return (len(issues) == 0, issues)


def format_report(issues):
    """Human-readable summary, suitable for a CLI or a maintenance loop's log."""
    if not issues:
        return "Lesson store OK -- no integrity issues found."
    by_code = collections.Counter(i.code for i in issues)
    lines = [
        "%d integrity issue(s) found (%s):"
        % (len(issues), ", ".join("%s=%d" % (c, n) for c, n in sorted(by_code.items())))
    ]
    for i in issues:
        lines.append("  [%s] lesson %s: %s" % (i.code, i.lesson_id, i.detail))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="memory.db", help="path to the sqlite memory store")
    args = ap.parse_args()
    conn = memory_store.connect(args.db)
    ok, issues = check_store(conn)
    print(format_report(issues))
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
