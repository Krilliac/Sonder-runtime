---
name: sonder-validation-and-qa
description: >-
  Evidence and QA doctrine for Sonder Runtime: what counts as proof, the hermetic
  pytest harness contract, how to add tests, shrink-only acceptance ratchets, and
  the execution-grounded eval inventory. TRIGGER when the user asks "is this
  verified", "what counts as evidence", "did the tests really pass", "add a test",
  "write the proof", "eval the model", "which tests should I run", or "why was my
  test skipped". DO NOT TRIGGER for venv creation, dependency install, or making
  pytest collect at all — that is sonder-build-and-env; for CI gate ordering and
  merge policy use sonder-change-control; for post-mortems of specific past
  failures use sonder-failure-archaeology.
---

# Sonder Runtime: validation, evidence, and QA

This repo treats verification as a first-class deliverable. A green suite is
"expected, not impressive — say what you *verified*, not what you believe"
(`CONTRIBUTING.md:15-16`). This skill tells you what the repo accepts as
evidence, how the test harness actually behaves, how to add tests it will
accept, and which eval suites gate model changes.

Scale at commit 99162cf9: 803 `test_*.py` files under `tests/` + `proposals/`
(765 flat in `tests/`, 28 in `tests/production/`, 1 in `tests/live/`, 9 in
`proposals/`), 8,087 collected-shape `def test_` functions. Full-suite cost:
the recorded measurements (500.62 s in
`.superpowers/sdd/work/outcomes-source-report.md:234`, 523.76 s and 490.22 s
in the regression-selector validation runs) are **dated historical figures**
taken at earlier revisions collecting ~6,280-6,412 tests. Expect materially
longer at HEAD — `tests/production/test_architecture.py` alone measured
1195.39 s serially at 99162cf9. Re-time your own run and record whether
`-n 2` was used; do not quote the historical figures as current cost.

## When NOT to use this skill

| You want | Go to |
|---|---|
| Create venv, install requirements, fix import/collection errors | `sonder-build-and-env` |
| CI gate order as merge policy, branch/PR rules | `sonder-change-control` |
| Why a past incident happened; recurring failure patterns | `sonder-failure-archaeology` |
| Package boundaries the architecture checker enforces | `sonder-architecture-contract` |
| A live bug you are diagnosing right now | `sonder-debugging-playbook` |
| Selfmod candidate lifecycle and its gates | `sonder-selfmod-lifecycle` (its source is `SELFMOD.md`); this skill covers only the test-surface rule below |

## 1. The evidence doctrine (the repo's own rules)

### Checkbox rules — master spec

`docs/architecture/SONDER-MASTER-IMPLEMENTATION-SPEC.md:26-33`, verbatim
obligations:

- `[ ]` means unverified, **even if related code already exists**.
- `[x]` requires committed evidence named in the item or linked from the
  evidence log.
- A feature is not complete merely because a class, test double, proposal, or
  alternate legacy path exists.
- Deleting obsolete behavior is part of completion, not optional cleanup.
- Documentation-only completion never proves runtime completion.

### Evidence ledger — how a checkmark is backed

`docs/architecture/EVIDENCE-TRACKING-DESIGN.md` defines the machine-readable
authority behind every checked requirement
(`docs/architecture/evidence/requirements.jsonl`, append-only by revision):

- A `verified` record requires exact **baseline and verified 40-hex Git SHAs**
  and **at least one evidence item** (lines 74-78). Only `verified` permits
  `[x]` (line 93).
- Allowed statuses: `planned`, `in_progress`, `blocked`,
  `implemented_unverified`, `verified`, `regressed`, `superseded`, `rejected`
  (lines 82-91). A newly observed regression becomes `regressed` — never a
  preserved stale checkmark (line 139).
- The checker must "Never execute arbitrary commands embedded in the evidence
  ledger" (line 122). It validates structure and linkage; it does not replace
  tests.

Run it (verified exit 0 at 99162cf9):

```
python scripts/check_requirement_evidence.py
```

### The house verification discipline

Distilled from this repo's own work reports (`.superpowers/sdd/work/`), which
are the working examples of what a proof looks like here:

1. **Reproduce before fixing.** "Reproduced the failure, fixed it, the new
   test fails without the fix" is the merge-worthy shape
   (`CONTRIBUTING.md:16-17`).
2. **RED at the final count.** Show the new test failing *behaviourally*
   against the pre-fix code, at the final item count — not an earlier draft
   (`.superpowers/sdd/work/app-permissions-report.md:189` "RED (3 items, at
   the final item count, before any fix)").
3. **Control case.** "A guard that refuses everything is not a guard"
   (`.superpowers/sdd/work/fix-critical.md:48`). Every refusal test needs a
   companion showing the guard *permits* the legitimate case.
4. **Mutation-prove every new guard.** Plant a defect, watch the guard catch
   it, revert, verify restoration. See
   `.superpowers/sdd/work/attribution-residuals-report.md:200` ("every guard
   planted, observed failing, reverted") and
   `.superpowers/sdd/work/dead-vocab-report.md:199`.
5. **A count of exactly 0 or 1 is the classic fabrication tell.** Corroborate
   it independently — `fix-critical.md:199-200` corroborates a 0 "three ways".
   Ask what the number would look like if the check had silently stopped
   checking.
6. **Floors versus totals.** A count taken before the read completes is a
   floor; a count that skips failures is a ceiling reported as a total
   (`outcomes-source-report.md` "Note on the zeros" section explains why a 0
   must ship beside the total it was reclassified out of).
7. **Name what you ran.** Every report names its exact test files and states
   explicitly when the full suite was not run (e.g.
   `.superpowers/sdd/work/dead-vocab-report.md:329`).
8. **Never present static reasoning as a test result.** Say which one you
   have.

## 2. The test harness contract

Two conftest layers create a hermetic, offline suite. Read them before
touching anything they control.

### Root `conftest.py` (repo root — runs before collection)

- Creates a throwaway state dir `sonder-pytest-*` and points `SONDER_HOME`,
  `SONDER_DB`, `SONDER_FLEET_DB` at it; cleaned at session end
  (`conftest.py:15-35, 63-82`).
- **Offline sentinels**: `SONDER_SERVER`, `SONDER_LOCAL_FALLBACK` =
  `http://127.0.0.1:1` and `OLLAMA_HOST=127.0.0.1:1` — port 1 refuses
  instantly, so any test that accidentally reaches for a live model fails
  fast instead of hanging (`conftest.py:31-33`). Also `SONDER_ALLOW_CLOUD=0`,
  `SONDER_WEB_TOOLS=0`, `SONDER_EMBED_CACHE=0` (the embed cache would couple
  tests through shared state — cache tests opt back in).
- **Autouse `_isolate_deployment_posture`** saves/restores
  `SONDER_AUTH_MODE`, `SONDER_API_KEY`, `SONDER_REQUIRE_ACCOUNT` around every
  test, because production entrypoints deliberately export resolved config
  into `os.environ` and that leak once gave the whole session a false
  "authenticated deployment" posture (`conftest.py:39-60`).
- **Marker gating uses `get_closest_marker`, never `item.keywords`**:
  keywords also contain parametrize *values*, so a test parameterized with
  the literal string `"model"` was once silently skipped though never marked.
  "A security assertion that skips itself reads exactly like a passing one"
  (`conftest.py:101-113`). Preserve this if you ever touch collection hooks.

### `tests/conftest.py`

- Pins `SONDER_FLEET_DB` to an isolated ledger **before test modules import**,
  and deliberately never restores it in-process — a late atexit callback must
  never fall back to the operator's live database (`tests/conftest.py:19-33`).
- Autouse `_isolate_fleet_ledger` calls `fleet_store.clear_all()` before each
  test: a leaked in-model-call agent once made unrelated learning tests defer
  distillation (`tests/conftest.py:39-54`).
- Autouse `_configure_http_legacy_boundary` monkeypatches
  `serve._LEGACY_RUNTIME = server` (and the REPL equivalent) plus
  `OllamaGateway.configure_default_providers(...)` — the same explicit
  injection the serve bootstrap performs, rebound per-test so runtime doubles
  cannot leak across xdist workers (`tests/conftest.py:57-78`).
- Opt-in fixture `without_standing` strips the measured-standing prefix from
  agent end reports; use it only in tests about something else —
  `tests/test_agent_verification_gate.py` owns asserting the prefix itself
  (`tests/conftest.py:89-112`).

### Markers and the single live test

`pytest.ini` declares `testpaths = tests proposals` — proposals ship in the
desktop payload, and omitting them once let their production-facing
compatibility tests silently disappear from bare CI (`pytest.ini:2-4`).
Markers: `unit`, `integration`, `network`, `model`. `network` and `model`
tests are **skipped unless** you pass `--run-network` / `--run-model`
(options defined in `conftest.py:85-98`).

Exactly one test in the repo talks to a real model:
`tests/live/test_model_gateway_live_smoke.py`. It self-skips unless
`SONDER_LIVE_MODEL_GATEWAY` is `ollama` or `openai`; remote endpoints
additionally require `SONDER_LIVE_ALLOW_REMOTE=1`; it asserts the literal
`SONDER_GATEWAY_OK` in the response (lines 23-39). Bare CI only collects a
skip and never calls a provider.

### Running the suite

```
# Everything offline (what CONTRIBUTING asks for before a PR):
venv/Scripts/python -m pytest -q

# What CI runs (.github/workflows/ci.yml:35):
python -m pytest -q -n 2 --dist load --durations=20

# One file / any pytest args, interpreter resolved from the repo's own venv:
scripts\run-tests.cmd tests\test_foo.py
scripts\run-tests.cmd -q -k pattern

# Opt in to gated markers:
python -m pytest -q --run-network
python -m pytest -q --run-model

# The live smoke, explicitly:
# set SONDER_LIVE_MODEL_GATEWAY=ollama first
pytest -m "model and network" tests/live/test_model_gateway_live_smoke.py --run-model --run-network
```

`scripts/run-tests.cmd` exists because a venv quoting failure once cost a
lane its entire verification step; its exit-code semantics and the full story
live in `sonder-build-and-env`. Use it when a bare `python -m pytest` gives a
"No Python at ..." error.

**Known-red baseline at 99162cf9** (verified by execution, 2026-08-23): a
bare full suite fails exactly two tests —
`tests/test_remaining_doc_001_005.py::test_authority_checker_passes_and_inventory_is_complete`
and `::test_public_generator_freshness_check_passes` — because four generated
files (`runtime-reference.json`/`.md`, `architecture-map.json`/`.md`) are
stale at that commit; `scripts/check_documentation_authority.py` exits 1 for
the same reason. A red run there is the baseline, not your change. The
production-scope remedy (not applied on the skill-forge branch) is
`python scripts/generate_documentation_catalogs.py --write`. Details:
`sonder-docs-and-writing`.

## 3. Adding a test

### Where it goes

| Kind | Location / naming |
|---|---|
| Normal behavior test | `tests/test_<subject>.py`, flat in `tests/` (765 files follow this) |
| Architecture-from-the-test-side | `tests/test_<subject>_boundary.py` — 33 such files enforce package boundaries |
| Production/release hardening | `tests/production/` (28 files: ratchets, migrations, redaction, update engine, entrypoints) |
| Proposal compatibility | beside the proposal in `proposals/<name>/` (collected via `pytest.ini` testpaths) |
| Live provider smoke | `tests/live/` — marked `model` + `network`, self-skipping |

### Meta-tests: the checkers are themselves mutation-tested

`tests/production/test_architecture.py::test_checker_detects_a_violation`
copies `sonder_runtime/` and the checker into `tmp_path`, `git init`s the
copy, plants a real violation file there, and asserts the checker fails —
never planting in the live tree (lines 318-360). If you add a new gate
script, add the matching plant-in-a-copy meta-test; a gate without one is
unproven ("a guard that cannot fail is not a guard",
`.superpowers/sdd/work/codegen-loop-report.md:132`).

### Monkeypatch-double discipline

From the repo-wide audit in
`.superpowers/sdd/work/ratchet-doubles-report.md:179-196`:

- Doubles that restate an explicit parameter list break silently when the
  real signature grows a keyword. **Write forwarding doubles as
  `*args, **kwargs`** unless the test asserts on a specific parameter. (1,231
  existing doubles lack `**kwargs`; that is legacy convention, not license to
  add more.)
- **Avoid negative assertions that survive feature deletion.** Eleven
  negative assertions were once guarded by sink doubles that could not raise
  (commit `de4be5d`, report line 286) — the assertions passed for the wrong
  reason. Pair every "X does not happen" with a positive proof that the
  mechanism preventing X is alive.
- External-tool absence is not a pass: follow the `VerifierUnavailable`
  pattern (section 6) — distinguish "could not judge" from "judged and
  failed" in any fixture that shells out.

### Never weaken existing tests to make something pass

`selfmod.py:1015-1031`: the selfmod verifier computes
`weakened_surface = existing_tests & changed_files` and **rejects any
candidate that modified a pre-existing test file**, and separately rejects
when the before-inventory of test ids is not a subset of the after-inventory
("test inventory was weakened"). Human changes are held to the same
standard by review: if a change requires editing an existing test's
expectations, the PR must say why the old expectation was wrong. The
automated side of this gate lives in the selfmod lifecycle (`SELFMOD.md`;
sibling skill `sonder-selfmod-lifecycle`).

## 4. Selecting the regression set for a change

Never hand-pick test files by grepping for terms you thought of — that rule
previously selected 61 of 280 files and missed the one file that tested the
exact gate being moved (`scripts/select_regression_tests.py:6-14`). Use the
selector, which derives search terms from the diff's own changed module-level
identifiers:

```
python scripts/select_regression_tests.py                # working tree + unpushed commits vs @{upstream}
python scripts/select_regression_tests.py --since main   # a whole branch
python scripts/select_regression_tests.py --since main --format args   # paste into pytest
```

Contract (lines 26-33, 237-253; behavior confirmed by running it at
99162cf9):

- Searches `tests/` and `proposals/` (`TEST_DIRS`, line 46).
- **Exit 0**: selection produced; stderr reports `selected N of M` plus the
  list of changed identifiers **no test mentions** — that uncovered list is
  the number that matters most.
- **Exit 2**: `SELECTION VACUOUS` — no identifiers extracted or zero tests
  matched. This is an **infrastructure failure** (empty diff, wrong
  `--since`), and must never be read as "nothing to run". An earlier version
  of this tool diffed the working tree against itself and made committed work
  100% invisible while printing plausible counts — the "selected N of M"
  number shipped as a token artifact once
  (`.superpowers/sdd/work/sweep-of-the-fleet.md:247`; full story in
  sonder-failure-archaeology, incident 8).
- Selection is a floor, not a proof: for schema/storage changes, run the full
  suite anyway (`outcomes-source-report.md:233` did exactly that, twice).

## 5. Acceptance thresholds: shrink-only ratchets

Three checked-in ratchets act as acceptance thresholds. The shared rule —
the baseline may only shrink; migrate the site, never regenerate to go green
— is doctrine owned by `sonder-change-control` §4, along with its exemplar
incident. This section keeps the inventory.

| Ratchet | Command (all verified exit 0 at 99162cf9) | Baseline |
|---|---|---|
| Legacy `ERROR:` string signaling | `python scripts/check_error_signals.py` | `scripts/error_signal_baseline.json` (upper-bound universe: per-scope counts + AST-hash signals) |
| Git-history privacy debt | `python scripts/check_history_privacy.py --json` | `KNOWN_HISTORY_PRIVACY_DEBT` pinned in the script (7 object/path pairs at 99162cf9; deleting entries allowed, adding forbidden; `--require-clean` for tagged releases) |
| Root legacy modules | `python scripts/check_architecture.py` | `ROOT_LEGACY_MODULES = {"server"}`, `ROOT_LEGACY_MODULE_LIMIT = 1` (`scripts/check_architecture.py:63-70`) — "a ratchet, not a target" |

Notes that bite:

- `check_error_signals.py` tracks only two statically exact categories
  (returned `ERROR:`-prefix literals and `.startswith("ERROR:")` parsers) and
  says so (`scripts/check_error_signals.py:1-8`). `--print-baseline` exists
  for inspection; committing its output to absorb a new finding defeats the
  ratchet — its failure message is explicit: "remove/migrate sites, do not
  add or swap them". Known blind spot (open, documented in
  `ratchet-doubles-report.md` "NEW findings"): `ERROR:`-prefixed
  *assignments* are invisible to it, so rewriting a site out of the ratchet's
  universe is possible and must be treated as evasion in review.
- `check_history_privacy.py --json` reports `"clean": false` with
  `known_debt_count: 7` and still exits 0 in normal CI — the pinned set is
  permitted debt, not a pass. It examines names/object ids only, never blob
  contents (`scripts/check_history_privacy.py:1-11`).
- CI runs the checked-in gates in a fixed order before the suite; the order
  and merge policy are owned by `sonder-change-control` §2 (source:
  `.github/workflows/ci.yml:26-35`).

## 6. The verifier registry: execution-grounded pass/fail

`verifiers.py` is the single seam through which the solver/ladder/reward
loops judge artifacts: `verifiers.verify(name, artifact, spec) -> Verdict`
with fields `.passed/.reason/.detail` (line 32; `verify` at 241).

| Name | What it does | Unavailable behavior |
|---|---|---|
| `python_exec` | run code + assert-check in a subprocess | n/a |
| `program_run` | run a whole program headless, fail on real crash | n/a |
| `pytest_run` | run a repo's tests; can write the artifact under cwd first (traversal-guarded) | n/a |
| `typecheck` | mypy as a cheap partial oracle | raises `VerifierUnavailable` if mypy absent |
| `cpp_compile` | MSVC via vcvars64.bat | raises `VerifierUnavailable` if vcvars missing |
| `llm_judge` | model-graded rubric, threshold default 7/10 | weak oracle — only for non-executable outputs |
| `node_run`, `sql_valid`, `json_schema`, `ruff_check` | promoted external backends, registered defensively (lines 287-296) | see below |

The load-bearing rule (`verifiers.py:18-19, 45-46, 256-261`):
**`VerifierUnavailable` means "could not judge", which is distinct from
`Verdict(False)` "artifact failed" — a missing tool is never a pass and never
a fail.** External backends signaling unavailability MUST raise *this
module's* exception class; a same-named local subclass is silently missed by
`except verifiers.VerifierUnavailable`. `tests/test_verifiers.py` pins that
rule for every promoted backend. Verdict compatibility is structural
(`.passed/.reason/.detail`), not by shared class.

## 7. Eval and golden inventory (model quality gates)

All promotion-relevant evals are **execution-grounded**: pass/fail comes from
running the artifact, never from a model grading a model. `llm_judge` exists
only for non-executable outputs and gates nothing.

| Suite | What it measures | Run |
|---|---|---|
| `promotion_eval.py` | Deterministic SQL gate for model promotion. `SUITE_VERSION = "sql-promotion-v2"`, `REPORT_SCHEMA = "sonder.promotion-evaluation/v1"`, temperature 0, seed pinned (lines 22-29). Model emits one read-only SQL query, executed against disposable in-memory SQLite whose data is never in the prompt; reports keep bounded reason codes + artifact SHA-256 hashes, **never model responses or generated SQL** (docstring, lines 1-7). | via `eval_models.py` |
| `eval_models.py` | Base vs candidate on the promotion suite. Evaluation-only: "never copies/removes model aliases or changes runtime policy" (docstring). Acceptance (`promotion_eval.promotion_decision`, `promotion_eval.py:755-846`, called from `eval_models.py:44`): SQL floor (candidate score ≥ 3), structured-instruction probe must pass, **any per-task regression vs base rejects** (`task_regression:`), candidate must show lift when base is imperfect and stay perfect when base is perfect. Exit 0 accepted / 1 rejected / 2 history-recording error. | `python eval_models.py <base> <candidate> [--record-history]` |
| `eval_retrieval.py` | Retrieval-ON vs OFF grounded pass-rate on held-out tasks whose names are **disjoint from `training_tasks.TASKS`** (disjointness pinned by `tests/test_eval_retrieval.py`). No model calls at import. Chunk-resumable. | `python eval_retrieval.py [start] [count]` |
| `eval_solver.py` | pass@1 vs pass@repair on hard tasks — the lift the verifier buys at test time. | `python eval_solver.py [max_attempts]` (default 3) |
| `eval_duel.py` | Single-model vs cross-model repair strategies (rotate/critic), execution-verified. | `python eval_duel.py [n_tasks]` |

Eval commands above are read-verified against their docstrings and argument
parsing at 99162cf9; running them requires a local model and was not done
here.

### Evaluation history: identity-safe trends

`sonder_runtime/adapters/evaluation_history_store.py` (schema
`sonder.eval-history.v1`): append-only JSONL that "never runs an evaluation
or calls a model" and reports trends **only within an exact
model + model digest + suite + suite version + suite digest identity**
(docstring, lines 1-5; `make_identity`, line 63). Digests are validated
64-hex SHA-256; a record whose `identity_key` does not match its fields is
rejected. Consequence: never compare pass-rates across identities — a
requantized model or edited suite is a new identity, not a trend point.
`eval_models.py --record-history` verifies model digests before and after the
run and refuses to record on mismatch.

## 8. Reporting results: the required shape

When you claim verification in this repo, your report must contain:

1. The exact commands and test files run (node ids for single tests).
2. RED output before the fix, GREEN after, both at the final item count.
3. Mutation/control evidence for any new guard or checker.
4. An explicit "full suite was / was not run" statement with the reason.
5. For any 0, 1, or exactly-at-threshold number: how you corroborated it.
6. Checks not run and known limitations — stated, not implied.
7. For requirement checkboxes: the evidence-ledger record, with baseline and
   verified SHAs, appended only after validation passes
   (`EVIDENCE-TRACKING-DESIGN.md:127-139`).

Anything labeled here as `open` (the error-signal assignment blind spot) or
`candidate` is unproven by definition; do not cite it as settled behavior.

## Provenance and maintenance

Verified against commit 99162cf9 (2026-08-22). Re-verify volatile facts with:

- Counts: `find tests proposals -name "test_*.py" | wc -l` (803);
  `ls tests/*_boundary*.py | wc -l` (33); `ls tests/production/test_*.py | wc -l` (28).
- Gates: `python scripts/check_architecture.py && python scripts/check_requirement_evidence.py && python scripts/check_error_signals.py && python scripts/check_history_privacy.py --json`
  — exactly these four were exit 0 at 99162cf9 (privacy reports
  `known_debt_count: 7`). `scripts/check_documentation_authority.py` was
  **exit 1** at the same commit (four stale generated catalog files; see the
  known-red baseline note in section 2).
- Harness contract: `sed -n '17,35p;49,60p;101,113p' conftest.py` and
  `sed -n '19,33p;57,78p' tests/conftest.py`.
- CI command and gate order: `sed -n '26,35p' .github/workflows/ci.yml`.
- Selector exit codes: `python scripts/select_regression_tests.py --since main --format list; echo $?`
  (exits 2 vacuous when the diff is empty — confirmed live at 99162cf9 where main == HEAD).
- Promotion suite identity: `grep -n "SUITE_VERSION\|REPORT_SCHEMA" promotion_eval.py`.
- Ratchet limits: `sed -n '63,70p' scripts/check_architecture.py`.
- Selfmod test-surface rule: `sed -n '1015,1031p' selfmod.py`.
- Suite duration: re-time with `python -m pytest -q -n 2 --dist load --durations=20`
  and record the worker count. The prior measured full runs (500.62 s,
  523.76 s, 490.22 s) collected ~6,280-6,412 tests at earlier revisions;
  they are historical context, not the expected cost at HEAD.
