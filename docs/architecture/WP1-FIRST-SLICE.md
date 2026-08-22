# WP1 first migration slice: context overflow policy

**Status:** Focused verification passed; full-suite qualification pending
**Target requirements:** `ARCH-001`, `ARCH-002`, `ARCH-004`, `ARCH-010`, `CTX-007`
**Candidate root module:** `context_overflow.py`

## Why this is the safest first slice

`context_overflow.py` is a bounded, stdlib-only, deterministic module with:

- 412 production lines;
- only `re`, `dataclasses`, and `__future__` imports;
- one direct production caller (`server.py`);
- one dedicated test module (`tests/test_context_overflow.py`);
- two packaging/source-inventory references (`selfmod.py` and
  `scripts/package_local_system.py`);
- no database, filesystem, network, subprocess, environment, thread, or model I/O;
- no existing same-name package implementation to reconcile.

It therefore exercises the complete “move, rewire, delete, ratchet, package, test” WP1
pattern without starting with a persistent store, process lifecycle, permission gate,
or high-fanout subsystem.

## Proposed destination and split

Do not move the 412-line file wholesale under a misleading name. It currently owns two
related but distinct pure policies:

```text
sonder_runtime/domain/context/
  __init__.py
  overflow.py       error normalization and overflow classification
  compaction.py     deterministic emergency message compaction
```

The split makes the later `CompactionEngine` seam explicit: `compaction.py` is the
current emergency deterministic policy, not the final event-based compaction service.

## Exact implementation steps

- [ ] Record a clean full-suite and dedicated-test baseline in a qualified dev runtime.
- [x] Add `sonder_runtime/domain/context/__init__.py`.
- [x] Move classifier constants, `ContextOverflowMatch`, normalization, negative
  controls, numeric evidence, and `classify` into `domain/context/overflow.py`.
- [x] Move `COMPACTION_NOTE`, message role helpers, and `compact_messages` into
  `domain/context/compaction.py`.
- [x] Update `server.py` to import the two package modules explicitly.
- [x] Update the dedicated tests to import the package modules; preserve every behavioral
  assertion rather than adding a root compatibility import.
- [x] Update `selfmod.py` and `scripts/package_local_system.py` source/package inventories
  to reference the new files.
- [x] Delete root `context_overflow.py` in the same change.
- [x] Add an architecture regression test proving production code cannot reintroduce a
  root `context_overflow` import.
- [x] Run the dedicated tests, architecture checker, packaging tests, selfmod source
  inventory tests, full suite, Ruff, and history/privacy checks.
- [ ] Append evidence records, but keep master requirements unchecked unless the evidence
  proves the entire requirement rather than only this slice.

## Local verification record

Completed against the staged tree on 2026-08-19:

- [x] `python -m py_compile` for every changed Python module.
- [x] `python scripts/check_architecture.py`.
- [x] `git diff --check`.
- [x] Direct classifier/compaction smoke assertions, including negative-control veto and
  compaction idempotence.
- [x] Source search confirms no remaining root import or path reference.
- [x] Dedicated pytest and packaging pytest. The checkout's
  interpreter does not contain pytest, Ruff, or the runtime's `mcp` dependency, and the
  configured network cannot reach a package index. These checks remain required in CI or
  a qualified development environment.

Current focused verification: context/MMR/reward/execution selection is `113 passed`;
architecture, evidence, and staged-diff checks pass.

## Behavioral invariants

- Classification remains conservative and bounded to the existing byte budget.
- Status codes remain supporting evidence only.
- Explicit negative controls continue to veto incorrect compaction retries.
- Numeric requested/limit comparisons remain fail-closed.
- Emergency compaction preserves system messages, recent turns, the existing note, and
  idempotent “already compacted” behavior.
- No new environment reads, I/O, framework dependency, or import-time side effect.
- No compatibility shim remains at the root.

## Verification commands

```bash
python -m pytest -q tests/test_context_overflow.py
python -m pytest -q \
  tests/production/test_architecture.py tests/test_package_local_system.py
python scripts/check_architecture.py
python scripts/check_history_privacy.py
python -m ruff check \
  sonder_runtime/domain/context server.py tests/test_context_overflow.py \
  selfmod.py scripts/package_local_system.py
python -m pytest -q
```

Test filenames were resolved against the `6670eaa` baseline. Resolve them again after
live reconciliation before implementation and record any changed owning test in evidence.

## Stop conditions

Do not proceed with this slice if, after live reconciliation:

- another session moved or materially changed `context_overflow.py`;
- the starting test suite is red in the affected surface;
- packaging depends on root-relative import semantics not captured above;
- a different active branch already owns the same migration;
- the change would require a temporary root compatibility shim.

In any stop condition, refresh this preparation document from live evidence before
choosing another slice.
