# WP0 preparation baseline

**Captured:** 2026-08-19
**Repository:** `Krilliac/Sonder-runtime`
**Git baseline:** `6670eaabe8ddb8a35c0c01e067f54d89c7379aeb`
**Working-tree qualification:** Documentation consolidation is present but not committed;
no production source file is modified.
**Purpose:** Read-only inventory for planning WP1; this document does not claim that any
master-spec implementation requirement is complete.

## Reconciliation result

- Local `HEAD` and fetched `origin/main` both resolve to `6670eaa`.
- The consolidated master specification exists locally and contains 204 unique,
  machine-readable requirement IDs plus 46 work-package/acceptance checkboxes.
- The documentation change set is disjoint from production runtime code.
- The prior `migration-inventory.json` is a historical 2026-08-09 snapshot at
  `7d5aa62`; it must not be used as the current count/status authority.

## Current source inventory

| Measure | Current value | Collection boundary |
|---|---:|---|
| Tracked Python files | 752 | `git ls-files '*.py'` |
| Production Python files | 386 | tracked Python excluding `tests/` |
| Test Python files | 366 | tracked Python under `tests/` |
| Root Python modules | 173 | tracked `*.py` with no directory component |
| `sonder_runtime/` Python modules | 155 | tracked package Python |
| Architecture root-legacy allowance | 16 | `BASELINE_ROOT_LEGACY_MODULES` |
| Explicit compatibility root modules | 3 | `eval_history`, `memory_store`, `recall` |
| Root/package basename overlaps | 6 | listed below |
| Master-spec requirement IDs | 204 | unique `FAMILY-NNN` IDs |
| Master-spec checkboxes | 250 | requirements plus WP/acceptance tasks |

Root/package basename overlaps:

- `memory_store`
- `model_transport`
- `process_liveness`
- `recall`
- `selfmod`
- `workflow_store`

An overlap is not automatically a defect: some package modules intentionally replace a
root module while others use a broad basename in a different bounded context. Each must
be classified before deletion. The six names are mandatory WP1 review points.

## Architecture checker baseline

The checker currently exits successfully, but success proves only the migration ratchet,
not the master-spec end state. Its source still permits:

- 16 named root legacy modules;
- five root platform modules;
- root imports from adapters, bootstrap, platform, and the entry layer;
- three compatibility-root modules;
- one immutable migration import exception;
- a non-zero root legacy limit.

Therefore, `python scripts/check_architecture.py` is a useful regression gate but cannot
yet prove `ARCH-002`, `ARCH-003`, `ARCH-010`, or `ARCH-011`.

## Package skeleton status

Present package areas:

- Domain: common, automation, execution, memory, routing, runtime policy, selfmod,
  tools, training, and updates.
- Application: agents, automation, backup, chat, evaluation history, execution,
  inspection, memory, operations, output stream, preferences, preflight, recall,
  runtime policy, selfmod, tasks, training, updates, and workflows.
- Adapters: accelerators, execution, filesystem, Git, inference, observability,
  persistence/SQLite, training, updates, web, and transitional root-backed adapters.
- Interfaces: CLI, HTTP, MCP, and REPL.
- Platform and bootstrap packages are present.

This invalidates the old inventory's `missing_spec5_packages` section. Presence alone
does not prove that a package is authoritative or fully wired.

## Known transitional boundaries

### Root legacy modules allowed by the checker

`server`, `runtime_policy`, `embeddings`, `autopilot_store`, `fleet_store`,
`sonder_operations_store`, `sonder_migrations`, `sonder_lifecycle`, `sonder_secrets`,
`sonder_serve`, `sonder_repl`, `sonder_updates`, `sonder_update_engine`, `workbench`, and
`file_ops`.

### Root platform delegates

`sonder_config`, `sonder_paths`, `sonder_version`, `sonder_metrics`,
`sonder_shutdown`, and `sonder_logging` are recognized at the transition boundary
(the checker constant currently contains the six names even though older prose called
this a five-module group).

### Explicit compatibility modules

- `eval_history.py`
- `memory_store.py`
- `recall.py`

### Other visible transition mechanisms

- `sonder_runtime/adapters/strangler_services.py`
- root-backed platform modules under `sonder_runtime/platform/`
- root imports from multiple adapters
- the root `server.py` remains a major composition/transport owner
- schema epoch 1 migrations and an epoch 2 bridge coexist

## Persistence inventory

Epoch-1 migration registries exist for:

- `memory.db`
- `autopilot.db`
- `fleet.db`
- `operations.db`
- `queued_actions.db`
- `updates.db`

Epoch-2 adapter code introduces or consolidates:

- `automation.db`
- `memory.db`
- `operations.db`
- `selfmod.db`
- `training.db`
- `updates.db`
- per-domain `outbox_events`

The bridge backs up epoch-1 databases, initializes epoch-2 owners, migrates tasks from
memory to automation, and stamps schema epochs. WP1 must not delete a root store until
its data adoption, ownership, recovery, and future-schema behavior have direct evidence.

## Interface inventory

Package interfaces currently exist at:

- `sonder_runtime/interfaces/cli/commands.py`
- `sonder_runtime/interfaces/http/handlers.py`
- `sonder_runtime/interfaces/mcp/handlers.py`
- `sonder_runtime/interfaces/repl/handler.py`

Root transport/entry modules still exist, including `server.py`, `sonder_serve.py`,
`sonder_repl.py`, `sonder_client.py`, `sonder_headless.py`, and launch scripts. WP1 must
classify each as authoritative entrypoint, protocol delegate, or obsolete business path.

## Verification availability

- `python scripts/check_architecture.py`: **PASS**.
- Internal Markdown link validation: **PASS** during the documentation consolidation.
- `git diff --check`: **PASS** during the documentation consolidation.
- Pytest collection/full suite: **NOT RUN in this workspace** because the selected
  runtime has no `pytest` installed. This is an environment limitation, not a pass.
- Ruff: not re-run during this read-only preparation checkpoint.

Before WP1 code changes, use the repository's sealed/development runtime or install the
qualified development dependencies, then capture collection and full-suite baselines.

## Repeatable read-only collection commands

```bash
git fetch origin main --prune
git rev-parse HEAD
git rev-parse origin/main
git status -sb
git ls-files '*.py'
find sonder_runtime -type f -name '*.py' -print
python scripts/check_architecture.py
python -m pytest --collect-only -q
```

All future baselines must record the exact Git SHA, dirty paths, interpreter, platform,
and unavailable checks. Counts without their collection boundary are not evidence.
