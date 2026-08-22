# Cross-cutting persistence: outbox and compare-and-set foundation

`sonder_runtime/application/persistence/outbox_cas.py` defines a
transaction-neutral persistence boundary for state shared by sessions, jobs,
workflows, and future repositories. `TransactionNeutralRecord` is a versioned
aggregate snapshot with a monotonic revision. `OutboxEvent` is an immutable,
versioned event envelope tied to the same aggregate and revision.

`OutboxCASRepository.append` is the adapter contract: it accepts an expected
revision and must stage the record and outbox event as one atomic persistence
operation. A stale writer returns `None`, and therefore cannot partially update
the record or publish an event. The in-memory implementation is a
thread-safe reference adapter for focused tests; it makes no durability claim
and does not choose a transaction backend.

Both value objects defensively copy mappings and expose them read-only. Schema
versions newer than the supported version are rejected before storage, which
prevents silently interpreting future data. The existing session, job, and
workflow repositories remain unchanged; future adapters may implement this
port alongside them.

Focused coverage is in `tests/test_crosscutting_persistence.py`.
No formal specification checkbox is changed by this slice.
