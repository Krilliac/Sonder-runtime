# store_integrity

A read-only health check over a `memory_store` connection: detects dangling
`lessons_fts` mirror rows (`orphan_fts`), lessons that exist but were never
mirrored into FTS (`missing_fts`, so `fts_search` can never surface them),
lessons with NULL/empty/whitespace-only text, and lessons whose `embedding`
blob is non-NULL but fails to decode as a float32 vector (`embeddings.from_blob`)
or decodes to an empty vector. `check_store(conn)` returns `(ok, issues)` where
`issues` is a list of `Issue(code, lesson_id, detail)` namedtuples, and
`format_report(issues)` renders a human-readable summary.

It's valuable because `lessons`/`lessons_fts` is a hand-maintained mirror with
no DB-level foreign keys or triggers (per `memory_store.delete_lesson`'s own
docstring) — a crash mid-delete, a bug in any future write path, or a bad
embedding write can silently degrade `retriever.py`'s RRF fusion and semantic
search, or crash the solver's retrieval hot path on a malformed blob, long
after the row was written. This checker catches all of that cheaply, offline,
before it bites the self-repair loop.

Integration: run `python proposals/store_integrity/store_integrity.py --db
memory.db` as a periodic maintenance step (e.g. alongside `lesson_pruner` in
`curriculum_run.py` or a standalone cron), or `import store_integrity;
ok, issues = store_integrity.check_store(conn)` anywhere a live connection is
already open (e.g. a startup check in `server.py` or `sonder_serve.py`).
It is strictly read-only and made no changes to `memory_store.py`,
`embeddings.py`, or any other existing module.
