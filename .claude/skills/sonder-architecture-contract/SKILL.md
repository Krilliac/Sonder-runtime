---
name: sonder-architecture-contract
description: >-
  Load-bearing design decisions, invariants, and known-weak points of the
  Sonder Runtime architecture. TRIGGER when asked "why is the code structured
  this way", "which document is authoritative", "can I import X from layer Y",
  "why did check_architecture fail", "is it safe to add a compatibility shim",
  "where do new ADRs go", or "what invariant am I about to break". DO NOT
  TRIGGER for executing a decomposition slice or for a red check_architecture
  hit mid-slice (use sonder-server-decomposition-campaign), for CI gate
  mechanics (use sonder-change-control), or for doc upkeep (use
  sonder-docs-and-writing).
---

# Sonder architecture contract

This skill records WHAT the load-bearing architectural decisions are, WHY each
one exists, WHICH invariants are machine-enforced, and WHERE the system is
known to be weak. It is the map, not the marching orders.

**When NOT to use this skill.** If you are actually extracting a slice out of
`server.py`, use `sonder-server-decomposition-campaign` (the executable WP1
recipe). If you are wiring or debugging CI gates, use `sonder-change-control`.
If you are updating the documentation set itself, use
`sonder-docs-and-writing`. If you are working on the self-modification
pipeline's runtime behavior, use `sonder-selfmod-lifecycle`.

Glossary of terms used below:

- **Strangler migration**: incrementally moving code out of a legacy module
  into a new structure until the legacy module can be deleted.
- **Ratchet**: a check that only permits movement in one direction (removing
  legacy dependencies is always allowed; adding one is a policy change).
- **Composition root**: the module that wires everything together at startup —
  here, the legacy `server.py`.
- **CAS**: compare-and-swap; an atomic pointer update used for model rollback.

## 1. Document authority hierarchy

Defined in `docs/architecture/DOCUMENT-AUTHORITY-INDEX.md`. Order matters:

| Rank | Source | Authoritative for |
|---|---|---|
| 1 | `docs/architecture/SONDER-MASTER-IMPLEMENTATION-SPEC.md` | Unfinished requirement list and checkbox state. Line 22: "If they conflict with this specification, this specification wins." |
| 2 | Focused contract docs: `ARCHITECTURE.md`, `SECURITY.md`, `SELFMOD.md`, `TRAINING.md`, `CLIENT.md`, `MOBILE_HOST_CONTROL.md` (repo root) plus `docs/architecture/external-mcp-bridge.md`, `queued-action-lifecycle.md`, `refinement-transactions.md`, `tool-capability-registry.md` | Current product boundaries, subordinate to the master spec |
| 3 | `docs/architecture/evidence/requirements.jsonl` + `docs/architecture/generated/requirement-status.*` | What has been VERIFIED (not what is planned) |
| 4 | `PROGRAM-STATUS.md`, all `WP*-*.md`, all `REMAINING-*.md`, `SPEC-5-*.md` under `docs/architecture/` | Historical / planning evidence ONLY |

Hard rule: **never cite a rank-4 document as current status.** Historical docs
deliberately keep imperative language; the index exists precisely so that
language cannot be mistaken for authority. When a focused doc says "planned"
or "not implemented", that is not evidence the master-spec checkbox is done.

ADR namespaces (two exist — this is a real trap):

- `docs/adr/` — canonical directory for NEW ADRs. New files must be named
  `ADR-YYYY-MM-DD-<slug>.md`; date + slug is the globally unique identifier.
  The numbered `ADR-001..006` files already in it are retained SPEC-5-era
  material. Numbers are never reused.
- `docs/architecture/adr/` — historical directory holding the older numbered
  architecture-program ADRs (`ADR-001-modular-monolith.md` through
  `ADR-009-local-observability.md`, plus its own `README.md` with collision
  and supersession rules).

## 2. The six SPEC-5 ADRs (all Accepted 2026-08-09)

These are the load-bearing decisions. Each row is decision + why, verified
against `docs/adr/ADR-00N-*.md`.

| ADR | Decision | Why |
|---|---|---|
| ADR-001 inbound interfaces layer | `sonder_runtime/interfaces/` is the SOLE inbound protocol layer (protocol translation, auth, OperationContext creation, application-service invocation). No business workflows, no direct DB access, no infrastructure instantiation in interfaces. `server.py` is decomposed and eventually deleted. | Protocol handling was interleaved with business logic; a thin layer enforces the dependency rule: interfaces depend on application services, never on adapters directly. |
| ADR-002 no-compatibility policy | Legacy compatibility is NOT an objective. No permanent shims, no permanent `adapters/legacy/`, no root-level business-module delegates, no two production implementations of one domain. | Single trusted operator, not a public multi-tenant API. Internal Python import compatibility is unnecessary overhead; user DATA is preserved via explicit migration, not via obsolete interfaces. |
| ADR-003 startup capabilities | `--unrestricted-tools` and `--unrestricted-selfmod` are two independent frozen booleans parsed at bootstrap, injected into the services that need them. Immutable after startup; never toggleable via HTTP, MCP, model output, agent, automation, or config mutation. NOT placed in OperationContext. | Startup-only flags prevent privilege escalation through model output or API calls; keeping them out of OperationContext prevents forgery. Reliability controls (deadlines, cancellation, logging) stay active in all modes. |
| ADR-004 transactional outbox | Every state-owning DB (memory.db, automation.db, training.db, selfmod.db, updates.db) has an `outbox_events` table; state + event commit atomically in one SQLite transaction. A dispatcher polls unpublished events into operations.db with `UNIQUE(source_event_id)` dedup. No cross-DB transactions. No external broker (Kafka/RabbitMQ/Redis). | Without an outbox, events are lost on crash between state commit and publication. Delivery is at-least-once, aggregate-local ordering, idempotent consumers. |
| ADR-005 immutable training deployments | Deployments use immutable identities `sonder-personal:<run-id>`; never mutate `sonder-personal:latest`. Runtime policy owns active-model selection via CAS revision. Rollback = policy-pointer change. DeploymentService is the sole runtime-policy mutator; training can NEVER promote itself. | Mutable aliases make rollback ambiguous and make "training cannot activate itself" unenforceable. Immutable identities give deterministic history. |
| ADR-006 schema epoch 2 | A bridge release migrates all SPEC-3 state to SPEC-5 domain ownership and stamps `schema_epoch = 2`. The final runtime requires epoch 2; pre-epoch state fails with MigrationRequired. No legacy migration code in the final runtime — old sources archived for audit only. | Complete legacy-import removal requires one explicit migration boundary; the bridge release is the single adoption point, so `ROOT_LEGACY_MODULES` can reach 0 without abandoning user data. |

Naming trap: ADR-004 calls the projector "LocalEventDispatcher"; the
implementation class is `OutboxDispatcher` in
`sonder_runtime/adapters/persistence/sqlite/outbox.py` (alongside
`OutboxWriter`). Grep for the implementation name, not the ADR name.

## 3. Package layout and the dependency rule

`sonder_runtime/` — 653 tracked `.py` files (counted 2026-08-22):

| Layer | Files | Role |
|---|---|---|
| `domain/` | 106 | Pure policy. Imports domain + stdlib only. No I/O, no environment. |
| `application/` | 257 (44 subpackages plus top-level modules) | Use cases, orchestration. Imports domain + application. |
| `adapters/` | 209 | All I/O: SQLite, subprocess, network. Imports domain, application, adapters, platform. |
| `interfaces/` | 32 (`http/`, `mcp/`, `cli/`, `repl/`, `a2a/`, `editor/`) | Inbound protocol layer. Imports application + interfaces ONLY (never adapters). |
| `platform/` | 33 | Host concerns (config, metrics, version). Imports platform only. |
| `bootstrap/` | 14 | Composition. May import everything, including root legacy modules. |

Allowed edges (from `ALLOWED_PACKAGE_EDGES` in
`scripts/check_architecture.py`):

```text
domain      -> {domain}
application -> {domain, application}
interfaces  -> {application, interfaces}
adapters    -> {domain, application, adapters, platform}
platform    -> {platform}
bootstrap   -> everything; entry (package __init__/__main__) -> everything
```

Import cycles inside the package are rejected outright.

## 4. Machine-enforced invariants: `scripts/check_architecture.py`

641 lines; run it before claiming any structural change is safe:

```powershell
python scripts/check_architecture.py
```

Exit codes: `0` clean AND silent (no output at all); `1` violations, one per
stdout line plus a count on stderr; `2` package root not found. A clean run
prints nothing — if you see output, something is wrong.

What it enforces beyond layer edges:

- **I/O containment**: `sqlite3.connect` only in adapters. `subprocess` only
  in adapters, except `platform/system_profile.py`, `platform/version.py`,
  and `adapters/updates/engine.py`. Network modules (`urllib`, `socket`,
  `http`, `ftplib`, `smtplib`) only in adapters (and entry, which is the CLI
  until it moves under `adapters/cli`), except `platform/config.py` (parses
  configured Ollama URLs) and `domain/ollama_policy.py` (pure URL parsing).
  Every exception is a NAMED file, not a directory.
- **Environment isolation**: `os.environ` / `os.getenv` / `os.environb` reads
  are forbidden in domain and application. Why: policy code must be
  deterministic and testable; the environment is an adapter concern.
- **The legacy-root ratchet**: `ROOT_LEGACY_MODULES == {"server"}` with a
  frozen `BASELINE_ROOT_LEGACY_MODULES` and `ROOT_LEGACY_MODULE_LIMIT = 1`.
  Source comment, verbatim intent: this is "a ratchet, not a target" —
  removing a legacy root dependency is always allowed; adding one requires an
  explicit architecture-policy change and must never happen as an accidental
  convenience import.
- **Retired modules stay retired**: `RETIRED_ROOT_MODULES` lists 52 exact
  paths (e.g. `sonder_serve.py`, `sonder_repl.py`, `file_ops.py`,
  `embeddings.py`, the `npu_*.py` family, `sonder_updates.py`,
  `live_reload.py`, and five retired `sonder_runtime/adapters/...` paths).
  A retired path is a violation BY FILESYSTEM PRESENCE, so the ratchet works
  even in a copied tree without `.git`. The list is deliberately explicit
  per-file, not a filename convention that could hide legitimate new
  entrypoints.
- **Exactly one approved shim**: `APPROVED_RETIRED_SHIMS` allowlists only
  `sonder_runtime/adapters/ollama/gateway.py`, byte-exact against a
  3-statement re-export (docstring, `from ..inference.ollama_gateway import
  OllamaGateway`, `__all__`). Any drift from those exact bytes trips the
  ratchet. Do not add entries here to "temporarily" keep something alive —
  that is the ADR-002 violation the check exists to catch.
- **Compatibility root modules**: 10 root modules (`archive_create`,
  `artifact_grounding`, `code_runner`, `command_catalog`, `fanout_store`,
  `learning_health`, `memory_store`, `autopilot_store`, `fleet_store`,
  `queued_actions`) that production callers must NOT import — import the
  packaged adapter instead. Allowlisted exceptions
  (`COMPATIBILITY_ROOT_IMPORT_EXCEPTIONS`): applied migrations
  (`migrations/*/0001_baseline.py`) because rewriting an applied migration
  invalidates its recorded checksum on deployed systems; `server.py` for
  `learning_health`; and five named legacy root consumers for
  `command_catalog`.
- **Inventory fails closed**: the file inventory comes from
  `git ls-files -z --cached -- *.py`, never a filesystem walk, so ignored
  build outputs can never become accidental inputs. If git fails, the checker
  raises instead of silently checking nothing.
- **Packaged-vs-root implementation split**: `web_tools.py` may hold only
  compatibility delegates; the `web_search` / `web_fetch` / weather /
  location implementations must live in their `sonder_runtime/adapters/`
  modules with named canonical entrypoints (`search_raw`, `fetch_raw`, etc.).

## 5. The meta-test: why green is trusted

`tests/production/test_architecture.py` (601 lines) exists because of the
project's core epistemic rule: a check that stops checking is
indistinguishable from a check that passes. It subprocess-runs the checker
and then attacks it:

- Hard-asserts `ROOT_LEGACY_MODULES == {"server"}` and that it stays a subset
  of the frozen baseline.
- **Mutation-tests the ratchet**: injects a fake legacy module reference and
  asserts the checker detects it.
- **Plants a real violation in a COPY of the tree** (`shutil.copytree` into
  tmp, fresh `git init` so the inventory works, never the live tree) and
  asserts exit code 1.
- Parametrized over 45 retired paths, asserting the exact
  "retired root module was reintroduced" message for each.
- Asserts the scan covered more than 100 files, with the literal failure
  message "the exclusion filter swallowed the source tree".
- Pins the sha256 of `migrations/memory/0001_baseline.py` (both LF and CRLF
  variants) — applied migrations are immutable historical artifacts.
- Asserts domain purity by direct source inspection, independent of the
  checker.

If you modify `check_architecture.py`, this test is the thing that keeps your
modification honest. Run both:

```powershell
python scripts/check_architecture.py
python -m pytest tests/production/test_architecture.py -q
```

## 6. The composition root: `server.py` and its containment rules

`server.py` is the last legacy root module: 25,325 lines, 537 top-level
function defs, 0 top-level classes, 206 `@mcp.tool()` registrations
(all counted 2026-08-22). Containment rules, each load-bearing:

1. **Only `sonder_runtime/bootstrap/legacy_root.py` may import `server`**
   from inside the package (plus the two named interface files
   `interfaces/http/serve.py` and `interfaces/repl/repl.py`, allowlisted in
   the checker). `legacy_root.py` exposes `runtime()` and a thread-safe
   `LazyRuntimeProxy` (double-checked locking on first access). Everything
   else reaches legacy behavior through that single boundary so the eventual
   deletion has one seam.
2. **Root redirect modules are pure aliases.** `fleet_store.py`,
   `memory_store.py`, `queued_actions.py` at repo root end with
   `sys.modules[__name__] = _implementation` pointing at the packaged
   adapter. They must never grow a second implementation — that would be two
   production implementations of one domain (ADR-002 violation).
3. **Hot-reload contract**: `server.py` must call
   `mcp.finish_module_refresh(__name__, __file__, globals())` (line 25309)
   BEFORE its entry-point code, and its `__main__` guard honors
   `_MCP_HOT_RELOAD_EXEC` (line 25324) so a reload re-exec does not restart
   the server.
4. **Staged atomic registry swaps**: `reloadable_mcp.ReloadableMCPServer`
   implements `begin_module_refresh` / `finish_module_refresh` /
   `abort_module_refresh`. Staging uses `threading.local` so concurrent
   tool-list calls never observe a half-built registry; abort keeps the last
   known-good registry; finish calls `command_catalog.reset_cache()`. Never
   add a second cache layered over the registry — the reset hook is the only
   sanctioned invalidation point.
5. **Live reload vs durable state**: `LIVE_RELOAD_MODULES` in `server.py`
   lists 76 process-local modules eligible for live reload.
   `master_orchestrator.py` keeps its process-local state behind
   `if "_LOCK" not in globals():`-style guards (lines 26-40) so a reload does
   not wipe in-flight orchestration. Durable ledgers (e.g. the packaged
   `fleet_store`) are deliberately NOT in the reload list — reloading a
   durable store buys nothing and risks connection state.

## 7. Cross-cutting invariants (each with its why)

| Invariant | Why it must hold | Where verified |
|---|---|---|
| Selfmod gates must be able to fail | A gate that cannot fail is a gate that fabricates approval. The smoke gate test carries the literal comment "the smoke gate must be able to fail". | `tests/test_selfmod_smoke_gate.py` line 106 |
| `selfmod_recover.py` imports no Sonder module | It is the out-of-tree rollback route; if selfmod breaks the tree, recovery must not depend on the broken tree. Docstring: "intentionally imports no Sonder modules". | `selfmod_recover.py` line 1 (stdlib imports only) |
| Tool-contract drift fails closed to admin | An unbound system operation answers `SYSTEM_OPERATION_UNBOUND` and is assigned role `admin` — an unmapped tool becomes maximally restricted, never accidentally public. | `tool_contract.py` (constant at line 47; admin fallback ~line 150) |
| `record_outcome` never accepts a caller-supplied `source` | `source` is required and keyword-only on `_record_outcome_and_maybe_distill` and deliberately NOT a parameter of the `record_outcome` MCP tool: "provenance a caller can choose is provenance a caller can misstate." Machine-graded results and caller judgement must stay distinct rows. | `server.py` ~line 3299 |
| Retrieval thresholds are calibrated, not chosen | `tune_min_sim.py` sweeps recall vs noise-rate over probe sets to recalibrate `retriever.DEFAULT_MIN_SIM`; a hand-picked constant (0.65 from a tiny corpus) demonstrably over-cut real hits. | `tune_min_sim.py` docstring; `tests/test_tune_min_sim.py` |
| Fleet children inherit principal / project / cancellation AT INSERT | The packaged fleet store raises `PermissionError` if a child's principal or project differs from its parent tree, and a child inserted under an already-cancelled parent is born cancelled ("cancelled with parent"). Inheritance at insert closes the race where a child outlives its parent's cancellation. | `sonder_runtime/adapters/persistence/fleet_store.py` ~lines 600-622 |
| Ollama pool fails over only pre-response | The pool is "an inference transport, not a distributed model runtime"; a completed request is never replayed on another host. Replay would double-execute side effects and double-learn from one interaction. Constants and scheduling: `sonder-agents-and-fleets`. | `sonder_runtime/adapters/inference/ollama_pool.py` docstring |
| Outbox dedup is a table constraint | operations.db imports use `UNIQUE(source_event_id)`; a duplicate dispatch after a crash is swallowed at the constraint, not filtered in Python. | `sonder_runtime/adapters/persistence/sqlite/outbox.py` lines 51, 152 |

## 8. Violation-message triage

When `check_architecture.py` prints a line, the fix is usually a placement
decision, not a suppression. Map the message to the intended move:

| Message contains | Wrong fix | Right fix |
|---|---|---|
| `may not import ... (adapters layer)` from interfaces | Adding the edge to `ALLOWED_PACKAGE_EDGES` | Route through an application service (ADR-001: interfaces never touch adapters) |
| `sqlite3.connect outside adapters` | A new named exception | Move the persistence code into an `adapters/persistence/` module and inject it |
| `subprocess outside adapters` / `network module ... outside adapters` | Extending the exception frozensets | Wrap the call in an adapter; exceptions are reserved for platform probes that predate the layer |
| `layer reads the environment (os.environ)` | Reading env in domain/application "just this once" | Read env in platform/bootstrap, pass the VALUE down as a parameter or config object |
| `retired root module was reintroduced` | Deleting the entry from `RETIRED_ROOT_MODULES` | Delete the file; its replacement already exists in `sonder_runtime/` (find it via the WP1 evidence doc for that slice) |
| `production caller imports compatibility root module` | Adding your file to the exceptions dict | Import the packaged adapter (`sonder_runtime.adapters...`) instead of the root name |
| `import cycle: A -> B -> A` | Deferring one import into a function body to hide it | Extract the shared piece downward (usually into domain) so both sides depend on it |
| `ROOT_LEGACY_MODULES grew from its ratchet limit` | Raising `ROOT_LEGACY_MODULE_LIMIT` | There is no approved second legacy root; reach `server` only via `bootstrap/legacy_root.py` |

The one legitimate reason to edit the policy constants is a COMPLETED slice:
removing a retired path that has been resurrected as a reviewed new
entrypoint, or shrinking an exception set after a caller migration. Both
directions of the ratchet tighten; neither loosens.

## 9. Known-weak points (stated plainly; all `open` as of 2026-08-22)

- **`server.py` is the last legacy root and a churn magnet**: 697 commits
  touched it in the past year (`git log --since=2025-08-22 --oneline --
  server.py | wc -l`, measured 2026-08-22). Every unrelated feature that
  lands there deepens the decomposition debt. The exit path is
  `sonder-server-decomposition-campaign`.
- **WP1 evidence-ledger adoption is incomplete**: the master spec's Evidence
  log table still contains a literal `_none yet_` row (line 671 of
  `SONDER-MASTER-IMPLEMENTATION-SPEC.md`), while 126 `.md` evidence records
  sit in `docs/architecture/evidence/` (128 entries counting the schema JSON
  and `requirements.jsonl`). Verified work exists that the formal
  ledger does not yet record — so rank-3 status queries under-report.
- **Stale agent branches**: 99 `origin/agent/*` remote branches at
  verification (`git branch -r | grep -c "agent/"`).
  `scripts/cleanup_merged_branches.py` exists; adoption is the gap.
- **The `mcp==2.0.0` exact pin** (`requirements-runtime.txt` line 6): the
  reload machinery (`ReloadableMCPServer`) subclasses the MCP server class,
  so any upgrade is a deliberate migration, not a routine bump. Candidate
  risk, not a current failure.
- **Two ADR directories** (section 1) will keep confusing tooling and humans
  until the historical series is fully cross-linked; always check both when
  searching for prior decisions.

## Provenance and maintenance

Verified against commit 99162cf9 (2026-08-22). All commands below were run
from the repo root on that date; re-run them before trusting volatile numbers.

- Architecture check passes clean: `python scripts/check_architecture.py; echo $LASTEXITCODE` (expect silent, 0)
- Meta-test: `python -m pytest tests/production/test_architecture.py -q`
- ADR statuses: `grep -n "Status" docs/adr/ADR-00*.md` (six files, all Accepted 2026-08-09)
- Layer file counts: `git ls-files "sonder_runtime/<layer>/*.py" | wc -l` per layer (106/257/209/32/33/14; 653 total)
- server.py stats: `wc -l server.py` (25325); `grep -c "@mcp.tool()" server.py` (206); top-level defs/classes via `python -c "import ast; t=ast.parse(open('server.py',encoding='utf-8').read()); print(sum(isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) for n in t.body), sum(isinstance(n,ast.ClassDef) for n in t.body))"` (537, 0)
- Ratchet policy sizes: `python -c "import sys; sys.path.insert(0,'scripts'); import check_architecture as c; print(len(c.RETIRED_ROOT_MODULES), len(c.COMPATIBILITY_ROOT_MODULES))"` (52, 10)
- server.py churn: `git log --since=2025-08-22 --oneline -- server.py | wc -l` (697)
- Stale agent branches: `git branch -r | grep -c "agent/"` (99)
- Evidence-ledger gap: `grep -n "_none yet_" docs/architecture/SONDER-MASTER-IMPLEMENTATION-SPEC.md` (line 671)
- MCP pin: `grep -n "mcp==" requirements-runtime.txt` (mcp==2.0.0)
