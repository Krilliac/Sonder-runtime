# Refinement transactions

`refinement_transactions.py` is a conflict-aware transaction layer over
existing Sonder state. It is not another learner, proposal queue, memory store,
or skill publisher. The first adapter targets existing SQLite-backed user
preferences and references existing grounded outcomes by interaction and signal.

Each explicit request supplies a target preference ID, its expected revision, a
typed bounded patch, evidence, and an expected outcome. The adapter takes a
SQLite `BEGIN IMMEDIATE` lock, checks the expected revision, writes through a
revision-qualified compare-and-swap, captures canonical before/after snapshots
and SHA-256 digests, and appends history in the same transaction. Validation or
conflict failures roll back both target and history.

Rollback names an apply refinement and the caller's expected current version.
It succeeds only while the preference exactly matches that refinement's
recorded post-state; it restores content as a new version and appends a linked
rollback event. Database triggers reject history update and deletion.

Current boundaries are deliberate:

- execution scope is local only;
- only non-executable preference text and enabled state are patchable;
- no model creates or applies a transaction automatically;
- improvement proposals are bounded references owned by the existing proposal
  workflow, not copied into a second queue;
- executable skills, code, permissions, roots, credentials, cloud policy, and
  runtime model policy are outside this adapter and can never be auto-published;
- history records the expected outcome but does not claim it was achieved;
  grounded outcome recording and review remain authoritative.
