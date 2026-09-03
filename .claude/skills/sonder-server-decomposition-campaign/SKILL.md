---
name: sonder-server-decomposition-campaign
description: >-
  Executable campaign runbook for finishing the WP1 strangler migration that
  decomposes server.py into the sonder_runtime package. TRIGGER when asked to
  "migrate a slice", "do a WP1 slice", "decompose server.py", "continue the
  strangler", "move to sonder_runtime", "retire a root module", or "shrink
  ROOT_LEGACY_MODULES". DO NOT TRIGGER for questions about why the
  architecture rules exist or which document is authoritative (use
  sonder-architecture-contract), for CI/commit gate mechanics unrelated to a
  slice (use sonder-change-control), or for debugging runtime failures (use
  sonder-debugging-playbook).
---

# Campaign: decompose server.py until ADR-001's end state is reachable

`server.py` is 25,325 lines, 537 top-level functions, zero top-level classes,
and 697 commits touched it in the year before 2026-08-22. It is the last
member of `ROOT_LEGACY_MODULES` in `scripts/check_architecture.py`
(`ROOT_LEGACY_MODULES = {"server"}`, ratchet limit 1, baseline frozen). The
end state, from `docs/adr/ADR-001-inbound-interfaces-layer.md` (Accepted,
2026-08-09): "server.py is decomposed and eventually deleted", with
`sonder_runtime/interfaces/` as the sole inbound protocol layer. Note the
file was 18,102 LOC when ADR-001 was written and 25,325 at commit 99162cf9 —
it grows while being migrated, so the campaign must outpace accretion.

283 `WP1-*-SLICE.md` documents exist under `docs/architecture/` at commit
99162cf9; the highest ordinal is `WP1-TWO-HUNDRED-NINETY-FIFTH-SLICE.md`.
Each campaign iteration is one **slice**: a small, behavior-preserving move
of one cohesive responsibility from a root module into the package, proven
by gates, documented, and landed. This skill is the loop you run.

**Vocabulary** (each defined once, used throughout):

- **Strangler migration**: incrementally moving live behavior into the new
  `sonder_runtime` package while the old `server.py` keeps working, until
  nothing is left to strangle.
- **Slice**: one such increment; behavior-preserving by definition.
- **Ratchet**: a checked-in limit that may only shrink (e.g.
  `ROOT_LEGACY_MODULES`, `RETIRED_ROOT_MODULES`). Removing a legacy edge is
  always allowed; adding one is a violation.
- **Identity-preserving alias**: a root name that still exists and `is` the
  packaged implementation (same object), so imports, monkeypatch seams, and
  hot reload keep working. Example: `fleet_store.py` is 13 lines ending in
  `sys.modules[__name__] = _implementation`.
- **Compatibility delegate**: a thin root wrapper that calls the packaged
  implementation (used when exact object identity is not required but the
  call surface is).

**When NOT to use this skill.** If the question is *why* the layer rules or
ratchets exist, which document wins a conflict, or what an ADR actually
decided, use `sonder-architecture-contract` — this skill assumes those
answers and just executes. If you are landing a change that is not a WP1
slice (commit conventions, CI gate order, ratchet policy in general), use
`sonder-change-control`.

## The ADR-002 tension, resolved

`docs/adr/ADR-002-no-compatibility-policy.md` says compatibility is not an
objective: no permanent shims, no `adapters/legacy/`, no two production
implementations. Yet nearly every landed slice "preserves the root
compatibility alias". These are consistent because the aliases are
**strangler scaffolding with a scheduled death**, not an objective:

1. A slice MAY preserve root import identity (alias or delegate) so it stays
   behavior-preserving and small.
2. A LATER slice retires the alias by adding the file to
   `RETIRED_ROOT_MODULES` once no production caller needs the root name
   (e.g. `sonder_metrics.py`, retired in the One-Hundred-Eleventh Slice).
3. Nothing NEW may depend on root names. The checker enforces this:
   `ALLOWED_ROOT_IMPORTS` is empty for `domain`, `application`, and
   `interfaces`, and `compatibility_import_offenders()` flags any production
   caller importing a `COMPATIBILITY_ROOT_MODULES` name outside its reviewed
   exception list.

If a slice would need a NEW temporary root compatibility shim (a root file
that does not exist today), stop — that is a listed stop condition.

## Phase 0 — eligibility and stop conditions

Run all three baseline gates from the repo root before touching anything.
All verified at commit 99162cf9 with Python 3.12.10 and pytest 9.1.1:

```bash
python scripts/check_architecture.py          # expect: NO output, exit 0
python -m compileall -q sonder_runtime tests  # expect: NO output, exit 0
git status --porcelain                        # expect: empty (clean tree)
```

Silence IS the pass signal for the first two. If `check_architecture.py`
prints anything, each line is one violation and stderr ends with
`N architecture violation(s)` (exit 1) — the tree is not a valid starting
point; fix or find who broke it before slicing. If it instead raises
`RuntimeError: tracked production source is missing or not a regular file`,
someone deleted a tracked file without staging the deletion (the checker
builds its inventory from `git ls-files` and fails closed).

Then run the test baseline for the surface you intend to touch (focused
suites first; the full suite belongs in Phase 3).

Quoted stop conditions from `docs/architecture/WP1-FIRST-SLICE.md` — do not
proceed if, after live reconciliation:

> - another session moved or materially changed [the target file];
> - the starting test suite is red in the affected surface;
> - packaging depends on root-relative import semantics not captured above;
> - a different active branch already owns the same migration;
> - the change would require a temporary root compatibility shim.

"In any stop condition, refresh this preparation document from live evidence
before choosing another slice." Check for a competing owner with
`git branch -a --contains` on recent slice commits and
`git log --oneline -10 -- server.py scripts/check_architecture.py`.

## Phase 1 — pick a slice

### Find candidates

```bash
grep -n "^def " server.py | wc -l        # 537 at 99162cf9; the backlog
grep -n "^def _" server.py | head -50    # private helpers: best candidates
git grep -n "_candidate_name" -- '*.py'  # fan-in; git grep = tracked files only
```

Read each candidate function. A good first-choice slice has the
`context_overflow.py` profile from WP1-FIRST-SLICE: bounded, stdlib-only,
deterministic, few callers, a dedicated test module, and "no database,
filesystem, network, subprocess, environment, thread, or model I/O". Pure
formatting/classification/policy helpers in `server.py` are the standing
supply; the last ~50 landed slices are mostly "moved pure X into the
packaged domain boundary while preserving the root server compatibility
alias".

**Split by responsibility.** WP1-FIRST-SLICE is explicit: "Do not move the
412-line file wholesale under a misleading name." If a candidate owns two
policies, create two packaged modules (that slice produced
`domain/context/overflow.py` and `domain/context/compaction.py`).

### Layer decision table

Destination layer is decided by what the code does, and the checker enforces
the edges (constants in `scripts/check_architecture.py`):

| Code does | Layer | May import (package) | Hard bans enforced |
|---|---|---|---|
| Pure policy, classification, formatting | `sonder_runtime/domain/` | domain only | any non-stdlib import; `os.environ`/`os.getenv` reads |
| Use-case wiring over ports | `sonder_runtime/application/` | domain, application | environment reads; sqlite/subprocess/network |
| I/O: sqlite, subprocess, HTTP, filesystem | `sonder_runtime/adapters/` | domain, application, adapters, platform | (sole home of `sqlite3.connect`, `subprocess`, `urllib`/`socket`/`http`) |
| Env, paths, process, config, version | `sonder_runtime/platform/` | platform only | network/subprocess except the reviewed module lists |
| Protocol translation (HTTP/MCP/CLI/REPL) | `sonder_runtime/interfaces/` | application, interfaces | importing domain or adapters directly |
| Composition root | `sonder_runtime/bootstrap/` | everything in the package | — |

Violation lines you will see if you choose wrong, verbatim from the checker:
`<file>: <layer> may not import <name> (<layer> layer)`,
`<file>: sqlite3.connect outside adapters`,
`<file>: subprocess outside adapters`,
`<file>: network module '<mod>' outside adapters`,
`<file>: <layer> layer reads the environment (os.environ)`.
Each names the fix (move the code, or invert the dependency); never widen
the allowance tables (see fences below).

### Reserve the next ordinal

Slice docs are English ordinal words: `WP1-<ORDINAL>-SLICE.md`. Find the
current maximum live (newest additions print first):

```bash
git log --diff-filter=A --name-only --format= -- 'docs/architecture/WP1-*-SLICE.md' | head -6
```

At 99162cf9 this shows the Two-Hundred-Ninety-Fifth batch, so the next doc
is `WP1-TWO-HUNDRED-NINETY-SIXTH-SLICE.md`. Re-check at execution time; a
parallel session may have taken the ordinal (ordinal collision = a stop
condition variant: re-number, do not overwrite).

## Phase 2 — the move recipe

The canonical pattern is "move, rewire, delete, ratchet, package, test"
(WP1-FIRST-SLICE). Numbered, with the exact files current slices touch:

1. **Create the packaged module(s)** in the layer chosen above, plus a
   focused test. Boundary tests are named `tests/test_<name>_boundary.py`
   (there are 33 `*_boundary.py` test files at 99162cf9; there is no
   `test_wp1_*` naming scheme). Copy the shape of
   `tests/test_launcher_output_boundary.py`: the first test asserts alias
   identity with `is`:

   ```python
   def test_root_helpers_are_identity_preserving_aliases():
       assert sonder_launcher._output_text is launcher_output.output_text
   ```

2. **Rewire the caller.** For server-owned helpers the landed idiom is an
   aliased import at the top of `server.py` (real example, server.py:207):

   ```python
   from sonder_runtime.domain.thinking_policy import (
       thinking_exhausted_budget as _thinking_exhausted_budget,
   )
   ```

   The private name keeps every existing call site and monkeypatch target
   working. For whole-module moves, keep the root file as the fleet_store
   pattern: import the packaged implementation and end with
   `sys.modules[__name__] = _implementation`.

3. **Delete the root file in the same change** when this is the retiring
   kind of slice (the root file has no remaining production reason to
   exist). Stage the deletion with `git rm <file>` (or `git add <exact-path>`
   for the deleted path) — never `git add -A`: this repo once left 83 MB of
   virtualenv packages untracked-and-unignored where a single `git add -A`
   would have committed them (the comment at `.gitignore:33` records the
   near-miss). An unstaged deletion makes the checker fail closed with the
   RuntimeError from Phase 0, not a violation line.

4. **Ratchet the retirement** (retiring slices only), in two places:
   - `scripts/check_architecture.py`: add `Path("<file>.py")` to
     `RETIRED_ROOT_MODULES`.
   - `tests/production/test_architecture.py`: add `"<file>.py"` to the
     parametrize list of `test_checker_rejects_reintroduced_migrated_root`.
     That meta-test plants the file in a copied tree and asserts the checker
     prints `<file>.py: retired root module was reintroduced` — proof the
     ratchet is not vacuously green.

5. **Update the two source inventories** WP1-FIRST-SLICE names, exactly as
   current slices do:
   - `selfmod.py` — `SENSITIVE_PREFIXES` is the selfmod control-plane
     protection list, not a packaging list. If the code you moved came from
     a protected root file (it lists `server.py`, `selfmod.py`,
     `tool_contract.py`, `model_transport.py`, ...), add the new packaged
     path so protection follows the code. Precedent already in the tuple:
     `"sonder_runtime/adapters/model_transport.py"`,
     `"sonder_runtime/domain/context/overflow.py"`,
     `"sonder_runtime/domain/context/compaction.py"`.
   - `scripts/package_local_system.py` — `ALLOWED_DIRS` already ships the
     whole `sonder_runtime/` tree, so new packaged files are included
     automatically. `REQUIRED_FILES` is a presence assertion checked at
     build and at manifest verification ("payload is missing required
     files" / "package manifest is missing required files"). If you deleted
     a root file listed there, remove its entry in the same change; if the
     migrated logic must be provably present in the desktop payload, add
     the packaged path (precedent: `"sonder_runtime/adapters/artifact_risk.py"`).
     `tests/test_package_local_system.py` also asserts retired roots stay
     out of the payload (e.g. `assert "eval_history.py" not in entries`).

6. **Preserve monkeypatch surfaces and module identity.** Anything a test
   or hot-reload path patches must resolve to the same object after the
   move. Whole-module: `sys.modules` alias (step 2). Single function:
   underscore alias import. Cross-check with
   `git grep -n "monkeypatch.setattr" tests/ | grep <name>` before assuming
   nothing patches it.

7. **Behavior stays identical.** No new environment reads, no import-time
   side effects, no output changes. If you found a bug while moving code,
   record it in the slice doc and fix it in a separate commit after the
   slice lands.

## Phase 3 — proof gates, in order, with expected observations

Run from the repo root. Every "silent" below means literally zero stdout.

1. `python scripts/check_architecture.py`
   - Expect: silent, exit 0 (verified at 99162cf9).
   - One violation line instead → the message names the edge and the layer;
     fix by moving code or inverting the dependency. **Never widen
     `ALLOWED_PACKAGE_EDGES`, `ALLOWED_ROOT_IMPORTS`,
     `PLATFORM_SUBPROCESS_MODULES`, `PLATFORM_NETWORK_MODULES`, or the
     sqlite rule** — that converts the gate into decoration.
   - `... retired root module was reintroduced` → you recreated a forbidden
     path (a retired filename, or a retired package path such as
     `sonder_runtime/adapters/strangler_services.py`). Pick a different
     packaged home; the only sanctioned exception is a byte-exact entry in
     `APPROVED_RETIRED_SHIMS`.
   - `RuntimeError: tracked production source is missing ...` → stage your
     deletion (Phase 2 step 3).
2. `python -m compileall -q sonder_runtime tests`
   - Expect: silent, exit 0 (verified). Any output is a syntax error with
     file and line.
3. Focused tests, then full suite:
   ```bash
   python -m pytest -q tests/test_<name>_boundary.py tests/production/test_architecture.py
   python -m pytest -q     # full suite
   ```
   - The production architecture module alone is slow: its reintroduction
     meta-test copies the 13 MB package tree and runs the checker in a
     subprocess per retired module. Measured at 99162cf9 on the machine
     recorded in the provenance block: `70 passed in 1195.39s (0:19:55)`.
     Budget for it and
     never kill it mid-run — a killed suite is not a passing suite.
   - Full suite: ~490-523 s reported on the machine recorded in the
     provenance block (2026-08-22, Ryzen 9 9900X3D), at earlier revisions
     with a smaller collected set. That reported figure is SMALLER than the
     measured
     time of its architecture subset above, so at least one of the two
     numbers does not describe your run conditions — time your own run and
     record what you observed, not either prior figure. Name what you
     actually ran in the slice doc; if you ran only focused suites, say so
     and mark full-suite qualification `open` exactly as WP1-FIRST-SLICE
     did.
   - A failure in a test you did not touch → your alias broke an identity
     or monkeypatch surface; diff the failing test's imports against your
     rewiring before suspecting the test.
4. `git diff --check`
   - Expect: silent, exit 0 (verified). Output = whitespace errors to fix.
5. `python scripts/check_requirement_evidence.py`
   - Expect: silent, exit 0 (verified). Recent slice docs list this gate.
6. `python scripts/check_history_privacy.py`
   - Expect: exit 0. This one is NOT silent; at 99162cf9 it prints
     `Git history privacy: known debt only (7 object/path pair(s)); release remains blocked`.
     That line is a pass for slice purposes.
7. `python -m ruff check <changed files>` — **CI/qualified environments
   only.** Ruff is not installed in this checkout's interpreter (verified:
   `No module named ruff`); do not report a ruff pass you could not run.
   Say "not run: ruff unavailable locally", per the spec's rule that
   "absence of the environment is not evidence of passing".

## Phase 4 — document and land

1. **Write the slice doc** `docs/architecture/WP1-<ORDINAL>-SLICE.md`. The
   current template (see `WP1-TWO-HUNDRED-FIFTIETH-SLICE.md` and
   `WP1-TWO-HUNDRED-NINETY-FIFTH-SLICE.md`) is short:

   ```markdown
   # WP1 <Ordinal> Slice — <boundary name>

   ## Boundary

   <What moved where; which root names remain identity-preserving aliases;
    what deliberately did NOT move and why.>

   ## Evidence

   - `tests/test_<name>_boundary.py` verifies <specific assertions>.
   - `python -m pytest -q tests/test_<name>_boundary.py ...`
   - `python scripts/check_architecture.py`
   - `python scripts/check_requirement_evidence.py`
   - `python -m compileall -q sonder_runtime <changed root files>`
   - `git diff --check`
   ```

   List only commands you ran; label anything else `open`.
2. **Append the README notes** (both files, verified conventions):
   - Root `README.md`: one appended bullet in the WP1 block at the end,
     e.g. `- WP1 Two-Hundred-Fiftieth Slice: read-only memory-quality
     doctor policy now lives in sonder_runtime.bootstrap.doctor_checks,
     preserving the root _check_memory_quality compatibility delegate.`
   - `docs/architecture/README.md`: one bullet
     `- WP1 <Ordinal> Slice: \`WP1-<ORDINAL>-SLICE.md\` — <summary>.`
     (The index lags the docs — it stops at the Two-Hundred-Fiftieth while
     283 slice docs exist and the highest ordinal is the
     Two-Hundred-Ninety-Fifth — appending your line is still required.)
3. **Commit.** Verified subject style from slice-landing commits:
   imperative sentence, no type prefix — `Migrate WP1 command policy and
   agent boundaries` (18404d7a), `Advance WP1 runtime migrations and
   evidence ledger` (85e14bdf), `Complete WP1 compatibility seam migration`
   (dbc9918a). One slice per commit; do not bundle behavior changes.

## Wrong paths — fenced off

| Never do this | Why (mechanism in the tree) |
|---|---|
| Regenerate or relax any ratchet baseline | `test_legacy_root_allowlist_has_a_shrink_only_ratchet` asserts `ROOT_LEGACY_MODULES == {"server"}` and mutates the set to prove the checker catches growth; `test_error_signal_ratchet.py` and `scripts/check_error_signals.py` guard theirs. Ratchets shrink only. |
| Add to `ROOT_LEGACY_MODULES` | Checker emits `ROOT_LEGACY_MODULES added non-baseline module(s)` and `grew from its ratchet limit of 1`; the checker's own comment: "adding one ... must never happen as an accidental convenience import". |
| Create `adapters/legacy/` or any second implementation | ADR-002: "no permanent adapters/legacy, no root-level business-module delegates, no two production implementations of the same domain". `strangler_services.py` and `legacy_model_gateway.py` are already in `RETIRED_ROOT_MODULES`. |
| Widen `ALLOWED_PACKAGE_EDGES` / subprocess / network / sqlite exceptions | Each existing exception (`PLATFORM_SUBPROCESS_MODULES`, `PLATFORM_NETWORK_MODULES`, `DOMAIN_PURE_URL_MODULES`, NPU/updates externals) is a reviewed, path-exact allowance with a comment explaining it. A convenience widening survives forever and is how the last 24 root modules died. |
| Touch an applied migration | `test_applied_memory_baseline_remains_byte_for_byte_immutable` pins `migrations/memory/0001_baseline.py` to two SHA-256s (LF and CRLF checkouts). The checker comment: rewriting one "would invalidate its recorded checksum on deployed systems". That is also why migration files keep legacy `import memory_store` lines — leave them. |
| Edit selfmod-protected files in an automated selfmod run | `selfmod.py` raises `PermissionError("protected paths require an explicit maintenance run: ...")` for `SENSITIVE_PREFIXES` paths unless the run is maintenance-authorized. Manual branch work is not gated, but moved protected code must be re-listed (Phase 2 step 5). |
| Change behavior inside a slice | Slices are behavior-preserving by definition; every landed slice doc says what it preserved. A behavior fix hidden in a 40-file move is unreviewable and unbisectable — separate commit, after the slice. |
| Run gates and skim them | A check that stops checking fabricates the reassuring value. Silence + exit 0 is the pass for the checker and compileall; anything else means read it. |

## Solution menu for hard knots (ranked, all patterns already in the tree)

1. **Helper tangled with module globals** → extract the pure core to
   `domain/` and keep a thin wrapper in `server.py` that reads the globals
   and passes them as arguments. This is the majority pattern of the
   landed 200-series slices ("moved pure X into the domain boundary while
   preserving the server compatibility alias").
2. **Circular import between root and package** → lazy import at the
   boundary, exactly like `tool_contract.py`:

   ```python
   def _server():
       import server
       return server
   ```

   Call it inside functions, never at module top.
3. **State that must survive hot reload** → module-level guards, as
   `master_orchestrator.py` does ("Preserve process-local execution state
   across importlib.reload()"):

   ```python
   if "_LOCK" not in globals():
       _LOCK = threading.RLock()
   ```

   `server.py` itself uses the same idiom for its MCP server:
   `_existing_mcp = globals().get("_PERSISTENT_MCP")` (line ~3753) reuses
   the live `ReloadableMCPServer("sonder-runtime")` across reloads.
4. **Tool registry decorators** → the live registration path is the
   `@mcp.tool()` decorators in `server.py` (first at line ~4703) on that
   persistent server. `sonder_runtime/interfaces/mcp/handlers.py` contains
   thin handler classes (parse input → OperationContext → service → error
   map), but at 99162cf9 no production code wires them into registration —
   they are the destination seam, `open`, not the live path. Until an
   interfaces/mcp slice owns registration, a tool's `@mcp.tool()` wrapper
   stays in `server.py` and only its body's policy/IO moves out.
5. **Whole root module with monkeypatch consumers** → `sys.modules`
   identity alias (`fleet_store.py`), then a later slice retires the alias
   once `compatibility_import_offenders` would report zero callers.

If none of these fit — the knot spans threads, persistence, and the
composition root at once — shrink the slice until one fits. Every one of
the 283 landed slices was small; no exception has been needed yet.

## Provenance and maintenance

Verified against commit 99162cf9 (2026-08-22), branch
`claude/fable-skill-forge`, Python 3.12.10, pytest 9.1.1, on Windows 11.
Commands verified by execution: `check_architecture.py` (silent, exit 0),
`compileall` (silent, exit 0), `git diff --check` (silent, exit 0),
`check_requirement_evidence.py` (silent, exit 0), `check_history_privacy.py`
(exit 0, known-debt line), and
`python -m pytest -q tests/production/test_architecture.py`
(70 passed, 1195.39 s). Not verified by execution here: the full pytest
suite (timing is the reported 490-523 s) and ruff (not installed locally).

Re-verify the volatile facts before relying on them:

```bash
wc -l server.py                                   # 25,325 at 99162cf9
grep -c "^def " server.py                         # 537 at 99162cf9
grep -n 'ROOT_LEGACY_MODULES = ' scripts/check_architecture.py   # {"server"}
grep -c 'Path(' <(sed -n '/RETIRED_ROOT_MODULES/,/})/p' scripts/check_architecture.py)
ls docs/architecture | grep -c 'WP1-.*SLICE'      # 283 at 99162cf9
git log --diff-filter=A --name-only --format= -- 'docs/architecture/WP1-*-SLICE.md' | head -3
python scripts/check_architecture.py; echo "exit=$?"             # silent, 0
python -m pytest -q tests/production/test_architecture.py        # slow; see Phase 3
```

If `server.py` is gone and `ROOT_LEGACY_MODULES` is empty, the campaign is
won: archive this skill and update sonder-architecture-contract.
