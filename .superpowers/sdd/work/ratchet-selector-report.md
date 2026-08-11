# Ratchet + regression-selector report — `work/29-ratchet-selector`

Branch `work/29-ratchet-selector`, from `06c2f79`. Commits: `8d8c289`, `3df7aa4`.

## Lineage (verified, not relayed)

```
git merge-base --is-ancestor 9f377f1 HEAD   -> 0   (fleet base IS an ancestor)
git merge-base --is-ancestor 2cec327 HEAD   -> 0   (the root-confinement fix IS an ancestor)
git merge-base HEAD work/16-dead-vocab      -> 9f377f1
```

Base is `feat/verified-fetch-modes-calibration` @ `9f377f1`, confirmed. `merge-base(HEAD, main)`
is `f018265`, 37 commits back — `main` is not the base.

**Correction to the brief (Important).** `scripts/select_regression_tests.py` **does not exist on
this lineage.** It was added by `3e27ae6`, which *descends from* `9f377f1` but is **not an ancestor
of it**:

```
git merge-base --is-ancestor 3e27ae6 HEAD   -> 1   (NOT an ancestor)
git merge-base --is-ancestor 9f377f1 3e27ae6 -> 0
branches containing 3e27ae6: work/16-dead-vocab, work/20-standing-planmode,
                             work/21-zero-callers, work/23-sweep-tonight
```

All four hold the identical blob `3e8d63c`. I imported that exact blob and fixed it, so the commit
diff is the fix alone. Any lane on the base lineage that reported running the selector could not
have been running it.

## Item 1 — the ratchet

### Reproduction (unpiped)

```
python scripts/check_error_signals.py   -> EXIT=1
legacy ERROR: signal ratchet failed; remove/migrate sites, do not add or swap them
server.py:14557: unexpected return_literal_prefix in _agent_dispatch (1 present, baseline allows 0)
```

`tests/production/test_error_signal_ratchet.py::test_checked_in_error_signal_universe_has_no_growth_or_swaps`
fails with the same finding. The baseline was **not** regenerated.

### Ruling: the pinning test protects REAL behaviour

Three tests assert the substring `no host-selected project root`
(`tests/test_agent_dispatch_dev_tools.py:183` and `:264`,
`tests/test_harness_root_confinement.py:163`). They are not transcribing the implementation — they
pin **which of two locks answered**. `test_harness_root_confinement.py:160-162` says so outright:
*"Both layers refuse this call, so asserting only 'ERROR:' would stay green if either were deleted
— two locks that can only be tested together are one lock."*

Measured, not assumed. With only lock 1 bypassed (`_DEVELOPER_WORKFLOW_TOOLS` emptied):

```
LOCK1-REMOVED secret_scan root='.' -> secret scan: 32 finding(s) in 924 files scanned
                                      admin_auth.py:24 [Secret/password] SECRET = "sonder-local-dev-secret
LOCK1-PRESENT secret_scan root='.' -> ERROR: read-only agent run has no host-selected project root...
```

Lock 2 (`harness_tools._resolve_root`) does **not** refuse `root="."`, because Sonder's own cwd is
in `file_ops.allowed_roots()`. So lock 1 is the only control for that shape, and the message is the
only thing distinguishing it. Neither the lock nor the assertion can be deleted.

### Migration

The repo documents its own idiom for this checker at `server.py:14505-14508`, in
`_agent_permission_gate_error` — the function immediately above the offending site:

> `# Assigned rather than returned as a literal: scripts/check_error_signals.py`
> `# ratchets new literal-prefixed ERROR: *returns*, and the agent loop's own`
> `# policy chain builds its HOST POLICY strings the same way.`

The condition and its message moved into a new gate helper `_agent_project_root_refusal`, which
`_agent_dispatch` now forwards exactly as it already forwards `_agent_permission_gate_error` and
`_repository_read_only_error`. Each refusal is then reachable from one place instead of restated at
the call site — the drift shape `dfad7ce` removed from `_agent_run_tool_refusal`.

Post-fix: `python scripts/check_error_signals.py` -> **EXIT=0**, empty output.

### Plant-and-revert (unpiped exit codes)

The first attempt was invalid and is reported as such: the plant files were written to the repo
root, where `production_files()` globs `*.py` and scanned the plants themselves, so the *reverted*
run also exited 1. Redone with the plants held outside the repo:

| plant | ratchet exit | reverted exit | reverted output |
|---|---|---|---|
| re-add the exact literal this commit migrated (`_agent_dispatch`) | **1** | **0** | 0 bytes |
| brand-new `ERROR:` return in a new scope | **1** | **0** | 0 bytes |
| `.startswith("ERROR:")` parser | **1** | **0** | 0 bytes |

Both categories covered.

## Item 2 — the selector (#55)

### Defect 1: it never diffed a commit range — reproduced

`changed_diff` read `git rev-parse HEAD` into `upstream` and used that variable only for its
truthiness, then diffed `git diff HEAD` — the working tree against itself. With commit `8d8c289`
(53 lines of `server.py`) committed and the tree clean:

```
default mode     : 0 identifiers,  0 of 321 test files, EXIT=2
--since 06c2f79  : 9 identifiers, 59 of 321 test files, EXIT=0
```

Same change, same tree. Committed work was **100% invisible**. Every "selected N of M" figure
produced that way is a floor.

### Defect 2: selection was token-driven — reproduced

`scan_tests` matched `\bTERM\b` against each test file's raw **text**, so comments, docstrings and
prose selected files. A **two-line** edit inside `check()` in `scripts/check_error_signals.py`:

```
selected 89 of 321 test files from 4 changed identifiers: check, inventory, load_baseline, violations
```

Per-term attribution of that 89: `check` **79**, `inventory` 14, `violations` 5, `load_baseline` 1;
everything except `check` = 17. One English word carried 81% of the selection.

(The first attempt at this reproduction silently no-op'd — a CRLF anchor miss made `str.replace`
return the string unchanged, and the run exited 2 on an empty diff. That exit-2 was **not** reported
as the finding; the edit was verified to land before the number above was taken.)

### The fix

- **Base resolution** is explicit, printed on stderr, and refuses to guess: `--since`, then the
  branch creation point from the branch reflog (`branch: Created from ...`), then `@{upstream}`,
  then `origin/HEAD`/`main`/`master`. The reflog rule is the only one right here — this lane forks
  at `06c2f79`, while `merge-base(HEAD, main)` is 37 commits back.
- **Matching runs over each test file's AST**: names genuinely referenced, attribute access counted
  only when its root is a name the file imported as a module, plus the string arguments of
  `setattr`/`getattr`/`patch` (how this suite reaches production symbols).
- **One-hop consumers**: a test names the caller, not the private helper a change touched.
- **Exit 3 on an over-broad selection** (`--max-fraction`, default 0.5).

Old text-matching vs new AST-matching, per term, over 321 test files:

| term | old (text) | new (AST) |
|---|---|---|
| `check` | 79 | **15** |
| `run` | 124 | **49** |
| `status` | 97 | **26** |
| `main` | 57 | **21** |
| `_agent_dispatch` | 28 | **27** |
| `REPOSITORY_READ_ONLY_TOOLS` | 25 | **24** |

The blowup case falls **89 -> 21**, while precise symbols barely move (28->27, 25->24). That is the
load-bearing evidence: this removes prose, not references. A selector that returns less is not
automatically better.

Default mode on this branch now: **0 of 321 (exit 2) -> 55 of 321 (exit 0)**, base reported as
`06c2f792cae1 (branch creation point (reflog)), 1 commit(s) in range`.

### Known-answer test — ground truth by execution

Planted mutation: three tools moved **out** of `REPOSITORY_READ_ONLY_TOOLS` (`file_read`,
`repo_diff`, `text_search`; gate 59 -> 56, control `file_digest` left in). Verified the mutation
landed before measuring — the first two attempts silently mutated the wrong occurrence and were
discarded.

Ground truth required the full suite. **Cost: 523.76s mutated + 490.22s clean baseline** (the
baseline is needed to subtract pre-existing failures). A selector cannot be validated by the count
it reports about itself.

```
clean   : 1 failed, 6280 passed, 46 skipped, 4 subtests passed in 490.22s
mutated : 19 failed, 6263 passed, 46 skipped, 4 subtests passed in 523.76s
failing FILES: 7 mutated - 1 pre-existing = 6 ground-truth catchers
```

| ground-truth catcher | selected |
|---|---|
| `tests/test_file_prefetcher.py` | yes |
| `tests/test_git_ignore_privacy.py` | yes |
| `tests/test_git_tools.py` | yes |
| `tests/test_master_orchestrator.py` | yes |
| `tests/test_read_only_agent_policy.py` | yes |
| `tests/test_tool_capabilities.py` | yes |

**Recall 6/6 = 100%. Selected 71 of 321 test files (22%) — 4.5x smaller than the full suite.**

**This did not pass first time, and only the known-answer test caught it.** At `3df7aa4` recall was
**4/6**: `test_file_prefetcher.py` and `test_master_orchestrator.py` were missed because policy here
is a set of tool *names* and those tests name the tool as **data**, never as a symbol —
`tools=("file_read",)`, `prefetcher.observe("file_read", {...})`,
`'{"tool":"text_search","args":{"query":"needle"}}'`. `6e8dabc` adds changed members of a
module-level collection literal as their own term source, matched against identifier-shaped tokens
inside each test's string constants. Confirmed this did not reopen the word scan: blowup case still
**21 of 321** and `check` alone still **15**.

### Vacuous / over-broad guards

| case | exit |
|---|---|
| over-broad, `--max-fraction 0.10` against a 17% selection | **3** |
| over-broad, whole-branch diff `--since 9f377f1` -> 162 of 321 (50%) at default 0.5 | **3** |
| vacuous, empty range + clean tree | **2** |

A silent empty result can no longer read as a pass, and neither can a selection so large it has
stopped discriminating.

## Verbatim pytest lines

```
RED  (ratchet)  1 failed, 222 passed in 10.29s
GREEN (ratchet) 223 passed in 9.32s
RED  (#48 F4)   1 failed in 0.35s      [_help_summaries the sole survivor]
GREEN (#48 F4)  30 passed in 4.08s
FINAL           375 passed in 26.35s   [10 files named deliberately, NOT via the selector]
clean full      1 failed, 6280 passed, 46 skipped, 1 warning, 4 subtests passed in 490.22s
mutated full    19 failed, 6263 passed, 46 skipped, 1 warning, 4 subtests passed in 523.76s
```

Gates: `check_error_signals` **0**, `check_architecture` **0**, `check_history_privacy` **0**.

## NEW findings

**Important — the selector is not on the fleet base.** See the lineage section. Four lanes hold it;
the base does not. Any base-lineage lane that reported "selected N of M" was reporting a figure it
could not have produced.

**Important — `tests/test_agent_tools.py::test_agent_runs_tool_then_final` is RED on this lineage
and is not mine.** It fails with
`TypeError: <lambda>() got an unexpected keyword argument 'repository_extra_roots'`: the test
monkeypatches `_agent_dispatch` with a lambda whose signature has drifted from the real one.
Evidence it predates me — my commit `8d8c289` touches only lines 14521-14593 (hunks
`@@ -14520,0 +14521,46 @@`, `@@ -14539,0 +14586,7 @@`, `@@ -14541,21 +14593,0 @@`), never
`_agent_dispatch_observed` at ~15909; the failing call site with `repository_extra_roots=project`
is present verbatim at `06c2f79`; `repository_extra_roots=project` was introduced by `7a4d0e9`.
Left unfixed deliberately — it is a shared test file owned by another lane, and editing it here
would create a merge conflict. It is the single pre-existing failure subtracted from the ground
truth above.

## #48 residual F4 — fixed

`command_catalog.reset_cache()` cleared **four of five** `lru_cache` readers (the brief said "only
`catalog`"; measured, it clears `catalog`, `console_tools`, `http_slash_tools`,
`_module_level_functions`). `_help_summaries` was cleared by nothing anywhere, so a live reload that
edited a `sonder_repl.py` HELP line kept serving pre-reload text for the process lifetime. Bounded
to summary text, not policy. One-line fix.

The new test is written against the decorator rather than a hand-kept list, so a sixth cache cannot
be added and silently missed. Guard binds by mutation: deleting the added line fails the test with
exactly `_help_summaries`.
