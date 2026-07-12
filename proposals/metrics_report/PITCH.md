# metrics_report

Read-only observability over `memory_store`: it groups stored lessons by
source-prefix (grounded from a real interaction vs synthetic batches like
`seed:curriculum:...` or `community`), breaks outcome signals down by count
and average reward, and computes a "distillation yield" — lessons produced
per good-outcome interaction — which surfaces how much `reflection.maybe_add_lesson`
is silently filtering (vague text, near-duplicate embeddings) without needing
to read logs.

It's valuable because `sonder_stats` today only shows raw totals and the 5
most recent lessons; this adds the *rates* (yield per interaction, yield per
good outcome, source mix) that tell you whether the learning loop is actually
converting good outcomes into reusable lessons, or mostly discarding them —
the kind of regression you'd otherwise only notice by eyeballing `seed_merge.py`
dry-run output.

To integrate: add a `metrics_report.build_report(conn)` / `format_report(report)`
call as a new `@mcp.tool()` in `server.py` (same `_open_db()` pattern as
`sonder_stats`), or call it directly from a maintenance script/cron for a
periodic health line in a log.
