# DATA-003/DATA-004 SQLite qualification

This evidence note records the DATA-004 qualification delta at baseline
`ce0291bf`. The recovery/hardening overlay used during qualification is
inherited context and is not claimed as authored by this lane.

## Acceptance boundary

DATA-003 requires state and durable outbox events to commit atomically in every
state-owning domain. DATA-004 requires revision checks on persistent workflow
and state-machine aggregates. The test uses the production
`SQLiteOutboxCASRepository`, `build_sqlite_persistence_facade`, and its actual
SQLite transaction context. It does not use a mock connection.

The typed persistence graph currently contains six canonical repositories:
`memory`, `automation`, `operations`, `selfmod`, `training`, and `updates`.

## Reproduction and result

Command, run serially with the repository virtual environment and isolated
pytest basetemp:

```text
python -m pytest -q tests/test_data004_multiprocess_qualification.py --basetemp=<isolated-temp>
```

Result: **3 passed in 4.28s**.

The tests demonstrate:

- Four independently spawned processes contend on the same aggregate in every
  one of the six domain repositories. Exactly one CAS winner commits and one
  outbox event remains after a separately spawned reader process reopens each
  database.
- A stale revision writer loses in every domain and leaves neither a changed
  aggregate nor a stale outbox event.
- A child process calls the production `repository.append` and a real SQLite
  `set_trace_callback` terminates it immediately before the production outbox
  INSERT, after the state write has executed. The process exit code is asserted
  as 17; reopen leaves no partial state or event in every domain, and SQLite
  integrity checks remain `ok`.

## Qualification status

The result is strong local evidence for the SQLite CAS/outbox boundary and
closes the previously missing multiprocess/stale-writer/crash-window test
coverage in this branch. It does **not** verify the full master requirements:

- DATA-003 still needs evidence that every other state-owning production domain
  uses the same atomic boundary and that external outbox delivery/recovery is
  correct.
- DATA-004 still needs hosted/final-head qualification for deployment-level
  contention and any non-SQLite persistence paths. This does not claim
  distributed PostgreSQL qualification.
- The test intentionally does not modify `requirements.jsonl`, master
  checkboxes, or generated requirement status. Root must independently audit
  and decide whether any status promotion is justified after exact-head CI.
