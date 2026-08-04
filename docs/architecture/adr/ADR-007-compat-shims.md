# ADR-007: Compatibility shims and deprecation policy

**Status:** accepted

## Context

Root modules (`runtime_policy.py`, `memory_store.py`, ...) have many
internal and external callers, including tests that reach private
helpers.

## Decision

Extraction keeps the root module as a delegating surface with identical
names and behavior (`runtime_policy.py` delegates to
`domain/runtime_policy/rules.py` and `adapters/filesystem/atomic_json.py`).
No new business logic is ever added to a shim. A shim is removed only in
a declared breaking release or after a documented compatibility period,
once internal imports, docs, clients, and plugins no longer use it.

## Consequences

Behavior-preservation is provable: the existing test suite runs
unchanged against the delegating module. Shims stay under a line limit
and contain no database/network/process code (CI-checked at sign-off).
