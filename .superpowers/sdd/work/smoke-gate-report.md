# Task #53 — a required approval gate that could not fail

Branch `work/26-smoke-gate`, from `4809f4e`. Third in the chain after
`selfmod-gate-report.md` (the `/selfmod` non-degradable gate) and
`deploy-health-report.md` (post-deploy rollback verification), which found
every item below as an out-of-scope sibling sweep.

Nothing here touches the live installation. Every reproduction and every
plant runs against a scratch Git repository under a temp directory with an
isolated `SONDER_SELFMOD_HOME`/`SONDER_SELFMOD_DB`. No `/selfmod deploy` or
`rollback` was executed, the memory DB was not opened, and the benchmark was
not run.

---

## 0. Lineage, verified rather than assumed

```
git merge-base --is-ancestor 9f377f1 HEAD  -> ANCESTOR_YES
```

`feat/verified-fetch-modes-calibration @ 9f377f1` is an ancestor of
`work/26-smoke-gate @ 4809f4e`. The five guarantees this branch is supposed
to carry are present in the tree, not merely in the log:

| guarantee | evidence in this checkout |
| --- | --- |
| `/selfmod` non-degradable gate | `39d78ca`, `permission_modes.non_degrading`, `server._SELFMOD_SOURCE_WRITING_ACTIONS` |
| permission gate | `tests/test_permission_gate_dispatch.py` present and passing |
| completeness floor over `_agent_dispatch` | `5b3ae1a`, `tool_capabilities.dispatch_names(_agent_dispatch)` |
| `harness_tools` root confinement | `2cec327`, `harness_tools._resolve_root` |
| strengthened post-deploy rollback verification | `0192d05` + `dafe831`, `selfmod.verify_rollback_ready` |

The anchor was re-resolved programmatically. `server.py` exists once in this
worktree, at the repository root; `find . -name server.py` returns exactly
`./server.py`. The circulated `app/build/` copy is not present in this tree
and no vendored file was modified (`git status` never listed `app/build`).

---

## 1. #53 — CONFIRMED. A candidate that should not pass smoke, approved.

`server.py:1766`:

```python
smoke = [sys.executable, "-c", "import pathlib; assert pathlib.Path('.').is_dir(); print('selfmod smoke ok')"]
```

`selfmod.record_test` runs each command with **the candidate workspace as its
working directory**, so `'.'` is always a directory that always exists. The
command never imports, runs, or reads one byte of the candidate. `passed` is
`exit_code == 0`, so the check was `True` unconditionally — and
`selfmod.review()` (`selfmod.py:797`) requires a *passing* check of kind
`smoke` before a candidate may be approved.

### Reproduction

A scratch repository whose entry point `boot.py` no test imports — which is
precisely what a smoke test exists to cover. The candidate fixes its declared
bug correctly *and* leaves `boot.py` raising at import:

```
candidate boot.py imports? NO -- exit=1 NameError: name '_undefined_helper' is not defined

PRODUCTION battery from server._selfmod_test_commands:
  syntax      [python, -m, py_compile, boot.py, calc.py]
  targeted    [python, -m, pytest, -q, tests/test_calc.py]
  regression  [python, -m, pytest, -q]
  smoke       [python, -c, "import pathlib; assert pathlib.Path('.').is_dir(); print('selfmod smoke ok')"]

recorded checks:
  syntax      exit=0   passed=True
  targeted    exit=0   passed=True
  regression  exit=0   passed=True
  smoke       exit=0   passed=True

review -> phase=approved last_error=''
approved_by='host:auto-low-risk'
```

`phase=approved`, with no operator present, for a candidate whose entry point
cannot be loaded. The battery came from `server._selfmod_test_commands`
itself, not from hand-written commands, so what is exercised is the real gate.

Note what the other three checks cannot see: `py_compile` proves a *parse* and
never executes module-level code; `pytest` only exercises what tests import.
The gap between those two is the entire reproduction.

---

## 2. Made smoke real, not removed — and why

I took option (a). Option (b) is legitimate when a real smoke test belongs
elsewhere, but here nothing else in the pipeline covers what `smoke` names,
and the reproduction above is exactly that uncovered gap. Removing it would
have closed the "gate that lies" problem by leaving the hole open. The
sibling lane's post-deploy probe does not close it either: that one verifies
*rollback readiness* after deployment, so catching an unrunnable candidate at
review time is strictly earlier and strictly cheaper.

`selfmod.record_smoke(run_id)` (`selfmod.py`), executed by a child process
rooted at the candidate workspace:

1. Imports every declared `.py` module that still exists — by dotted name
   where the path has one, else loaded from the file — so module-level code
   actually runs.
2. Confirms the import resolved *inside* the workspace, so a module shadowed
   by an installed package cannot answer for the candidate.
3. For every declared module the candidate deleted, confirms it is genuinely
   gone and no longer resolves.
4. Reports a SHA-256 over the paths and the bytes it actually loaded.

Against the constraints:

* **Cannot corrupt state when it fails.** It runs in a child process and
  writes nothing; a failure leaves the workspace exactly as it was, and the
  workspace is already isolated from the live repository.
* **No network, model, or operator.** stdlib `subprocess` and local files.
* **Fails loudly and specifically.** `boot.py: NameError: name
  '_undefined_helper' is not defined` — the module and the exception, not a
  status code.

**No proxy verification.** The expected receipt is computed by the *recording*
process from the workspace on disk and is deliberately never passed to the
probe. A probe reduced to `print('selfmod smoke ok')` exits 0 and is refused;
a probe printing a fabricated receipt is refused. "It returned 0" cannot stand
in for "it ran the candidate" — that is the same defect one layer up, and
`_record_command` now names it in the stored output when it refuses.

`server._selfmod_test_commands` no longer builds a `smoke` command at all.
The check lives in `selfmod.py` because the receipt must be computed by the
recording process, and because a command string in a list is too easy to
weaken from the caller.

---

## 3. Disposition of the three Importants and the Minor

| item | anchor | disposition |
| --- | --- | --- |
| 1. `syntax` degrades to a print | `server.py:1763` | **FIXED** |
| 2. migrate discards `--json`, trusts exit 0 | `sonder_update_engine.py:345` + `sonder_migrations.py:332` | **FIXED**, both halves |
| 3. tautological `required_kinds` | `assetgen.py:852`, `artifact_grounding.py:3587` | **FIXED** |
| Minor. syntax-only "VERIFIED" | `scripts/scaffold_verify.py:29` | **FIXED** |

**1.** The `.is_file()` filter emptied the candidate list exactly when the
change *deleted* its declared modules, and the required check became
`print('no Python syntax targets')` — exit 0, recorded as passing. Given this
repo's own measured finding that automated fix loops converge on deletion, the
one change shape a repair loop is most likely to produce was the one shape
that skipped the gate. An empty target set is now a refusal that names the
absent files. A *mixed* change (deletes A, edits B) still compiles B and lets
the new smoke gate verify A is really gone, so deletion is not blind — only
deletion-*only* runs are pushed to an explicit human review, which is the
intended trade and is documented in `SELFMOD.md`.

**2.** Two halves, both live. The engine requested `--json` and never parsed
it; `migrate_store` returned early on an empty discovery *before* the
`unknown`/checksum gates, so a release shipped without its `migrations/`
directory compared a database full of history against an empty known-set and
reported clean. The signal that the build had lost its schema definitions —
every ledger row now `unknown` — was computed by `status()` and discarded.
`migrate_store` now refuses that case; the CLI payload carries `discovered`,
`unknown` and `checksum_mismatches`; and the engine's `_migration_problems`
refuses an unparseable payload, an empty store list, pending work, an
unhealthy ledger, and zero migrations discovered across every store. This is
the treatment the adjacent `health_check` step already carried and the migrate
step had not been given.

**3.** Both sides read `manifest["kinds"]`, which is the *requested* kinds, so
`kind in kinds` compared a list with itself. `assetgen.KIND_ARTIFACTS` now
supplies what each kind must have left on disk, and the check compares against
files actually found. A required kind with no evidence map is a refusal, not a
pass.

**New, found while fixing 3 (Important):** the entire required-kinds loop sat
inside `if isinstance(manifest, dict)`, so a bundle with **no manifest** had
its `required_kinds` requirement silently dropped and still reported `ok` — a
caller asking for a check that was never run. The loop is hoisted to the
common path. Covered by
`test_required_kinds_are_enforced_on_a_bundle_with_no_manifest`.

---

## 4. Plant and revert — proof every gate can now fail

Each gate: baseline passes, planted violation refused, revert passes.

```
GATE 1  selfmod `smoke`
  A. baseline, sound candidate (must PASS)                   passed SELFMOD-SMOKE-RECEIPT 822fe93e...
  B. planted: entry point raises at import (must REFUSE)     REFUSED boot.py: NameError: name '_undefined_helper' is not defined
  C. planted: probe stubbed, exits 0 (must REFUSE)           REFUSED exit=0
  D. planted: probe prints a forged receipt (must REFUSE)    REFUSED exit=0
  E. mixed change, deletion verified gone (must PASS)        passed modules=1 gone=1
  F. reverted, sound again (must PASS)                       passed SELFMOD-SMOKE-RECEIPT 822fe93e...

GATE 2  selfmod `syntax`
  A. baseline, both modules present (must PASS)              passed
  B. planted: deletion-only change (must REFUSE)             REFUSED selfmod syntax gate: every declared Python target is absent
  C. planted: syntax error (must REFUSE)                     REFUSED
  D. reverted, sound again (must PASS)                       passed

GATE 3  update-engine `migrate`
  A. baseline, migrations discovered and applied (must PASS) passed
  B. planted: release shipped without migrations/ (REFUSE)   REFUSED no store discovered any migration
  C. planted: migrations still pending (must REFUSE)         REFUSED operations: still pending after migrate: 0002_next
  D. planted: ledger rows unknown to this build (REFUSE)     REFUSED operations: recorded but unknown to this build: 9999_future
  E. planted: exit 0, unparseable output (must REFUSE)       REFUSED migrate output was not readable JSON: 'migrations done!'
  F. reverted, healthy payload again (must PASS)             passed

GATE 4  `required_kinds`
  A. baseline, complete pack (must PASS)                     passed
  B. planted: 'icon' requested, nothing produced (REFUSE)    REFUSED bundle-required-kind: kind 'icon' missing icon.png
  C. reverted, complete again (must PASS)                    passed
  D. planted: required kind with no evidence map (REFUSE)    REFUSED kind 'icon': no kind_files evidence supplied

GATE 5  sonder_migrations.migrate_store discovery
  A. baseline, real migrations directory (must PASS)         passed applied=('0001_baseline',)
  B. planted: build lost migrations/ (must REFUSE)           REFUSED store 'operations' ... refusing to run
  C. reverted, directory restored (must PASS)                passed applied=('0001_baseline',)

GATE 6  scaffold_verify pyproject
  baseline: rc=0, rc=0;  planted malformed table header: rc=1 TOMLDecodeError at line 1, column 9
```

C and D on gate 1 are the anti-proxy cases: exit 0, refused anyway. B's
traceback names the scratch tree's own `boot.py`, which is the evidence that
the probe ran the candidate's bytes and not the installed ones.

---

## 5. TDD record

RED, at the final item count, before any production change:

```
21 failed, 29 passed in 34.64s
```

Each failed behaviourally — `AttributeError: module 'selfmod' has no attribute
'record_smoke'` for the new API, and for the pre-existing surfaces
`assert 'committed' == 'rolled_back'` and `assert False`, i.e. the defect
itself. The 29 that passed at RED include the non-degraded paths that were
always correct (`syntax` over surviving modules, `syntax` catching a real
syntax error), which is the expected shape.

GREEN, same four files, same 50 items:

```
50 passed in 28.23s
```

Full affected surface — selfmod, migrations, update engine, grounding,
assetgen, scaffolds, permission gate:

```
468 passed, 6 skipped in 126.33s (0:02:06)
```

Exactly what was run: `tests/test_selfmod_smoke_gate.py`,
`tests/test_required_kinds_evidence.py`, `tests/test_selfmod.py`,
`tests/test_selfmod_commands.py`, `tests/test_selfmod_deploy_gate.py`,
`tests/test_selfmod_deploy_health.py`, `tests/test_spec5_selfmod.py`,
`tests/production/test_migrations.py`, `tests/production/test_update_engine.py`,
`tests/production/test_entrypoint.py`, `tests/production/test_legacy_baselines.py`,
`tests/test_artifact_grounding.py`, `tests/test_artifact_grounding_server.py`,
`tests/test_assetgen.py`, `tests/test_grounding.py`, `tests/test_domain_grounding.py`,
`tests/test_scaffold_verify.py`, `tests/test_project_scaffold.py`,
`tests/test_update_manifest_trust.py`, `tests/test_update_schema_guard.py`,
`tests/test_safe_update.py`, `tests/test_spec5_updates.py`,
`tests/test_path_portability.py`, `tests/test_backup_service.py`,
`tests/test_preflight_service.py`, `tests/test_sonder_doctor.py`,
`tests/test_permission_gate_dispatch.py`, `tests/test_sonder_migration.py`.
The full suite (~522s) was not run. `ruff` is not installed in this
environment; the changed files were `py_compile`-checked instead.

### Tests read before being changed

Two existing tests began failing closed because they depended on the defect.
Both were read first and neither was weakened:

* `tests/test_artifact_grounding.py:169` passed `required_kinds` with no
  evidence map. Its `required_files` assertions are real and untouched; the
  tautological argument was given its evidence side. The test is now stronger.
* `tests/production/test_update_engine.py::_mini_source` answered
  `migrate --json` with `{"ok": true}`, which sufficed only because the engine
  discarded the payload. The stub now speaks the real contract. That is a
  harness under-specification corrected, not an assertion removed — every
  existing assertion in that file still holds.

---

## 6. Sweep for the same shape

AST sweep over the tree excluding `app/**` (vendored), `.git`, and generated
data. Shapes hunted: `assert <constant-true>`; a comparison of a value with
itself; a branch that prints instead of checking; a subprocess whose output is
requested and then discarded with only `returncode` read.

### FOUND

* `artifact_grounding` required-kinds dropped entirely for manifest-less
  bundles — reported in §3, fixed in this branch.

### CHECKED AND CLEARED

**`assert <constant-true>`: zero remaining in production code.** The `smoke`
command was the only instance in the tree, and it is gone.

**Compares-with-itself (4, all cleared).**
`proposals/prompt_clarifier/test_prompt_clarifier.py:199,200` and
`tests/test_project_detect.py:301` call the function twice and compare — these
are determinism assertions, which is the legitimate use of the shape.
`tests/test_retriever.py:640` is the inner comparison of an
`assert all(... for task in ...)` over 50 distinct tasks; the sweep flagged the
sub-expression, not a bare statement.

**Prints-instead-of-checking (72, all cleared).** All but one are trivial
programs used as *test payloads* (`print('ok')` fed to a runner under test),
which is their correct role. The one non-test hit,
`sonder_runtime/adapters/filesystem/workflow_store.py:26`, is a template
workflow whose description explicitly says "replace the code string" — a
placeholder, not a check.

**Output requested then discarded (12, all cleared).** False positives from
scope analysis: `grounding.py:409` returns `_combine(p)` (both streams),
`scripts/scaffold_verify.py:99` reads both via `_output_tail(result)`, and
`scripts/cleanup_merged_branches.py:29` returns the whole
`CompletedProcess` for its callers. Genuinely exit-code-only but correct:
`bootstrap_engine.py:116,145` (is the Ollama client able to reach the daemon),
`game_ladder.py:112` (is this interpreter usable), `sonder_headless.py:71`
(`python --version`). These are *liveness* probes, where the exit code is the
property being asked about, not a proxy for work done. The rest are in test
files asserting on process status deliberately.

**Noted, not filed.** `sonder_doctor.py:185` computes `overall` from
`worst = 0` seeded before the loop, so an empty `specs` list would render
`OK`. `specs` is a module-level literal list of checks and can never be empty,
so there is no reachable path; the `(no checks run)` line at `:200` is a
defensive *rendering* branch for a hand-built report, not a verdict. Recorded
here because it is the same shape one refactor away from being live.

---

## 7. Commits

```
7bdfa19  A required approval gate that could not fail: make selfmod smoke real (#53)
c8188e3  Read the migrate payload the update engine already pays for
c1c1475  required_kinds compared a value with itself; give it evidence
4e4a7a8  scaffold_verify: parse the pyproject.toml it printed VERIFIED over
```

Checkout left clean. Nothing pushed. `23-sweep-tonight`, `24-app-permissions`
and `25-login-grading` were not touched; no `git add -A` and no `git stash`
were used at any point.
