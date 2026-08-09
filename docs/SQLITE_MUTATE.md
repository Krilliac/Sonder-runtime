# Guarded SQLite mutation tool

`sqlite_mutate` executes exactly one parameterized `INSERT`, `UPDATE`, or
`DELETE` against an existing guarded SQLite database. It never accepts DDL,
PRAGMA, ATTACH/DETACH, functions, triggers, `RETURNING`, named
parameters, numbered parameters, `move`-style multi-statements, replacement
conflict actions, virtual tables, or their implementation shadow tables. Values
are supplied only as a positional JSON array matching bare `?` placeholders.

```json
{
  "path": "data/app.db",
  "sql": "UPDATE records SET status = ? WHERE id = ?",
  "parameters_json": ["done", 42],
  "mode": "preview"
}
```

Preview mode begins an immediate transaction, executes the statement, reports
the exact affected-row count, and rolls back. Apply mode must be selected
explicitly and commits the same single transaction atomically. A SQLite
authorizer permits only the selected DML action and main-database reads needed
for predicates; all functions and every other action are denied. Triggered or
cross-table mutations are rejected. Foreign-key enforcement remains enabled;
a statement requiring a denied cross-table cascade fails and rolls back.

The connection uses zero busy timeout, a monotonic progress deadline, SQLite
statement/parameter limits, row and database-size ceilings, and identity/path
revalidation before completion. WAL-mode apply uses a fail-closed worst-case
commit-frame projection and rolls back before commit when the combined storage
ceiling could be exceeded. The target and journal/WAL sidecars must be regular
non-symlink files inside authorized roots and outside sensitive or control-state
trees.

Defaults and hard maxima are: 1,000/10,000 affected rows, 2/5 seconds,
64/256 MiB database storage, 32,768 SQL bytes, 999 positional parameters,
128,000 parameter-input bytes, and 64,000 bytes per string parameter. The tool
is available to project-bound agents but deliberately excluded from every
autopilot policy; an attended caller must choose apply mode.
