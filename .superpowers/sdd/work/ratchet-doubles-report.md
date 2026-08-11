# Ratchet + doubles: #63, #64

Branch `work/32-ratchet-doubles`, base `fd699c3`. Four commits, clean checkout,
nothing pushed. No `git stash` (both preserved entries untouched), no
`git add -A`, no sibling worktree modified. The full suite was **not** run.
`scripts/select_regression_tests.py` is **absent on this lineage** (listed
`scripts/`), so regression files are named deliberately below. Every planted
file, mutation driver and pristine backup was held in the session scratchpad,
outside the repository.

## Lineage — verified in this worktree

`git merge-base --is-ancestor <x> HEAD` (rc 0 = ancestor):

| ref | ancestor of `fd699c3`? |
|---|---|
| `feat/verified-fetch-modes-calibration` (`9f377f1`) | **yes** |
| `604e662` (the inversion fix) | **yes** |
| `2cec327` (the root-confinement fix) | **yes** |
| `06c2f79` (fork point with lane 29) | **yes** |
| `8d8c289`, `dfad7ce` (the two cited precedents) | **no** — both live only on `work/29-ratchet-selector` |

Lane 29 forked at `06c2f79` and does **not** carry `604e662`
(`git merge-base --is-ancestor 604e662 work/29-ratchet-selector` → rc 1), which
is why it saw one ratchet finding and this branch sees two.

## ITEM 1 (#63) — the ratchet

### Reproduced before touching anything, unpiped

```
python scripts/check_error_signals.py > out.txt 2>&1   EXIT=1
  grounded_outcomes.py:197  startswith_parser in rendered_infrastructure_error
  server.py:14744           return_literal_prefix in _agent_dispatch
pytest tests/production/test_error_signal_ratchet.py   1 failed, 20 passed in 3.14s
```

The baseline was **not** regenerated and **no entry was added**.
`git diff scripts/error_signal_baseline.json` is empty across all four commits.

### `server.py:14744` — migrated, by cherry-pick

Lane 29 had already migrated this exact site in `8d8c289`, and its reasoning
holds on this lineage. Re-implementing it differently would have produced a
merge conflict against a sibling branch fixing the same defect, so it was
cherry-picked (`-x`) rather than re-authored: `1ab0038`, applied clean, diff
byte-identical to lane 29's.

The condition and its message move into `_agent_project_root_refusal`, which
`_agent_dispatch` forwards exactly as it already forwards
`_agent_permission_gate_error` and `_repository_read_only_error`.

I did **not** take the pinning test on trust. Lane 29 measured that with only
this branch bypassed, `_agent_dispatch("secret_scan", {"root": "."},
read_only=True)` actually ran and returned a secret in its text, because the
second lock does not refuse `root="."`. That measurement is reproduced in the
commit message and the message string is preserved verbatim; the three tests
asserting `"no host-selected project root"` are unchanged and green.

### `grounded_outcomes.py:197` — ruling: **structured read, not a baseline entry**

The brief allowed a narrowly-scoped baseline entry if the parse was genuinely
right. It was detecting a real thing, but it did not need the marker to do it,
so neither a baseline entry nor an evasion was necessary.

**Why the parse did not belong there.** `grounded_outcomes` is stdlib-only and
its `record_fn` is injected *specifically* so the module has no dependency on
the server (its own docstring says so). A hard-coded copy of the server's
`ERROR:` wire protocol is that dependency, spelled as a string instead of an
import. It was also a *second* protocol bolted beside the `key: value` header
grammar the same function already reads three lines below.

**What replaced it.** The leading line is read as what it is — an `error:`
header — which matches every refusal `_agent_dispatch` emits *and*
`_format_run_result`'s own `error:` field. One grammar, not two; the marker is
named nowhere.

**Why not simply delete it.** That was the tempting shrink and it is wrong.
Measured over the dispatcher's real refusal shapes, deletion agrees on 8 of 10
and diverges on the two that matter: a refusal with an empty detail
(`"ERROR:"` / `"ERROR: "`, which `server.isolated_run` renders for
`"ERROR: %s" % exc` when `str(exc)` is empty) is invisible to the header loop,
because that loop reads the *value* and the value is blank. Under deletion such
a tool is attributed a verdict it never produced — the exact harm the predicate
exists to prevent, and a false pass is the worse mistake here.

Planted the deletion as a mutation: **9 failed, 29 passed**; reverted, **38
passed**. Ten new pinning cases were added: every refusal shape the dispatcher
emits, the three empty-detail spellings, and a real rendered verdict that must
**not** be read as a refusal.

### Exit codes, unpiped, never through a pipe

| point | command | exit | bytes of output |
|---|---|---|---|
| before, at `fd699c3` | `python scripts/check_error_signals.py > f 2>&1` | **1** | 2 findings |
| after `1ab0038` (server site) | same | **1** | 1 finding |
| after `818a5e6` (both sites) | same | **0** | **0** |
| final, at `de4be5d` | same | **0** | **0** |

### Plant-and-revert — the ratchet still binds

Three plants, driver and pristine backups all held **outside** the repository;
no new file was ever created inside the scanned tree, so `production_files()`
could not scan a plant of its own.

| plant | exit | reported finding | after revert |
|---|---|---|---|
| re-add the **exact literal** `1ab0038` migrated, inline in `_agent_dispatch` | **1** | `server.py:14810 return_literal_prefix in _agent_dispatch` | **0**, 0 bytes |
| a brand-new `ERROR:` return in `grounded_outcomes.py` | **1** | `grounded_outcomes.py:238 return_literal_prefix in _planted_new_producer` | **0**, 0 bytes |
| the `.startswith("ERROR:")` parser this branch removed | **1** | `grounded_outcomes.py:238 startswith_parser in _planted_new_parser` | **0**, 0 bytes |

`git status --porcelain` empty afterwards.

## ITEM 2 (#64) — the doubles

### The three named doubles were **already fixed on this branch**

This is the correction to the brief. Measured, not inferred: `4f43bd7`
("Pin provenance by execution…", #62, already in `fd699c3`'s history) widened
all of them, and its own commit message says so — "Three over-narrow doubles
were widened rather than worked around". The #62 report filed them under NEW
findings as though open; both statements are in the same commit.

Current state on HEAD, read directly:

| site | double today |
|---|---|
| `tests/test_calibration.py:15` `calibration._counts` | `lambda *a, **k:` + provenance comment |
| `tests/test_agent_verification_gate.py:74, 815` `calibration._counts` | `lambda *a, **k:` |
| `tests/test_end_report_standing.py:53` `calibration._counts` | `lambda *a, **k:` |
| `tests/test_codegen_build_loop_server.py:145` `_record_outcome_and_maybe_distill` | `lambda *a, **k:` + the floor-as-total comment |

I did not re-fix them and I am not claiming them. What was still open is the
swallow behind them, and what the sweep found.

### The swallowed-exception path — fixed

`server._drain_deferred_distillations` swallowed every per-item exception with
a bare `except Exception: continue`. A batch whose items all raised returned
`stored: 0, deferred: 0` — byte for byte what a batch that legitimately stored
nothing looks like — and the campaign line printed "lessons stored 0, still
deferred in batch 0" with nothing saying the recorder never ran.

* `failed` counts raises. The exception is still absorbed — bookkeeping must
  not break the run it services — but no longer *silently*.
* `skipped` counts an unknown signal **and** a recorder that returned while
  claiming neither a lesson nor a deferral, which was a third silent bucket
  nothing named.
* The buckets now sum to the batch, pinned by a test driving all five endings.
* `_EMPTY_DRAIN` gives both early returns the full shape, so no caller can read
  a missing key — or a `.get` default — as a measured zero.
* `_drain_summary_text` renders the campaign line once instead of two identical
  format strings. The healthy line is byte-identical; the failure clause
  appears only when non-zero.

`tests/test_learning_tools.py`'s exact-dict pin broke. It was read before being
touched: its subject (a busy fleet blocks the drain) is unchanged, and it
pinned the short early-return shape incidentally. Kept as an exact dict —
that is what stops a bucket being dropped silently — and updated to the full
shape.

### Sweep for the same shape

**Silent handlers inside counting loops** — AST over every scanned production
file (repo root, `sonder_runtime/`, `scripts/`), looking for a `try` inside a
loop whose handler body is only `continue`/`pass` while the loop tallies a
counter. **6 sites.**

| site | verdict |
|---|---|
| `server.py:2961` `_drain_deferred_distillations` | **found — fixed** (above) |
| `sonder_runtime/.../bridge_migration.py:144` `_migrate_tasks` | **cleared** — `except sqlite3.IntegrityError` only, documented "already migrated", counter deliberately excludes it |
| `sonder_runtime/.../outbox.py:151` `dispatch_batch` | **cleared** — `except sqlite3.IntegrityError` only; a duplicate really is dispatched, so the increment is correct |
| `sonder_runtime/adapters/recall.py:90` `recall_page` | **cleared** — narrow `(TypeError, ValueError, EOFError)` on `embeddings.from_blob`; `scored_count` is reported as "scored", not as a total |
| `harness_tools.py:994` `secret_scan` | **found — reported, not fixed** (below) |
| `scripts/nightly_selfmod.py:413` `reclaim_orphans` | **found — reported, not fixed** (below) |

**Monkeypatched doubles** — AST over `tests/`, resolving each target by import.
**1,519** doubles carry an explicit parameter list; **1,231** lack `**kwargs`
and so cannot absorb a newly-added keyword argument — that is the fragile
shape, and it is repo-wide convention, not a defect I can close in this lane.
**35** of them sit in files this fleet's lanes have been changing. Of those,
the zero-argument doubles of zero-argument functions (`_open_db`,
`active_model_call_count`, `_maybe_live_reload`) are not drift-prone and are
cleared. Fixed:

| site | why it was over-narrow |
|---|---|
| `tests/test_agent_tools.py:1166, 1196` `_agent_dispatch_observed` | the two `#61` measured as the same latent trap and deliberately left; no assertion in either test concerns a parameter |
| `tests/test_agent_tools.py:1423` `_agent_validation_covers` spy | restated a parameter list it only needed to forward; now `*args, **kwargs` |
| `tests/test_grounded_outcomes.py:19` and `tests/test_grounded_outcomes_agent_dispatch.py:28` `_sink` | **the live instance the sweep was for** — see below |

### How a raising double can no longer read as zero

Two independent closures:

1. **At the drain**, a raise is counted (`failed`) and rendered
   (`_drain_summary_text`), so `stored == 0` can no longer be the whole story.
2. **At the sinks**, the double can no longer raise at all.

The second is the more serious find and it was not in the brief.
`grounded_outcomes.attribute`'s `record_fn` is doubled by `_sink` in two files,
and `server._feed_grounded_outcome` wraps the entire attribution in
`except Exception: pass`. **Eleven** of the assertions those sinks serve are
*negative* — `assert written == []`, the guards that stop a verdict being
invented for work nothing judged. A drifted double raises into that handler,
is swallowed, and reads as "nothing was written", which is exactly what those
guards expect.

Measured, with the original two-parameter sinks and one extra keyword at the
call site: **8 failed, 5 passed — and the 5 that passed were the negative
ones.** Both sinks are now `*a, **k`. Proved they still bind: with the sink
recording nothing, **8 failed, 5 passed**; restored, **13 passed**.

## Mutation results — every guard planted, observed failing, reverted

| guard | mutation | result |
|---|---|---|
| `rendered_infrastructure_error` leading read | deleted outright (the naive shrink) | **9 failed, 29 passed** |
| drain counts raises | `failed += 1` → bare `continue` | **3 failed, 26 passed** |
| drain counts the returned-nothing bucket | `else: skipped += 1` removed | **1 failed, 28 passed** |
| campaign line reports failures | failure clause dropped | **1 failed, 28 passed** |
| widened double a still binds | observation → an `ERROR:` refusal | **1 failed, 82 passed** |
| widened double b still binds | observation → an `ERROR:` refusal | **1 failed, 82 passed** |
| widened spy still binds | `covers` returns falsy | **1 failed, 82 passed** |
| sinks still bind | sink records nothing | **8 failed, 5 passed** |

Each reverted and re-verified; `git status` clean afterwards, no residue.

**Two survivals are worth recording as method, not just result.**

*The `skipped` mutation survived the first round (29 passed).* The accounting
test drove the unknown-signal path but never a recorder that returned normally
while claiming nothing, so that branch existed untested. The test now drives
all five endings and the mutation binds. A green suite did not show the gap.

*The first attempt at mutating doubles a and b survived (83 passed).* Making
the observation `""` changed nothing, because `_agent_observation_ok("")` is
`True` by deliberate design — the residuals report records that empty output is
valid for several inspection tools. The mutation was wrong, not the tests; an
`ERROR:` refusal is the value that should flip the file-evidence gate, and with
it both bind. Recording it because the first result would have read as "these
tests pin nothing" and that conclusion would have been false.

## Verbatim pytest lines

RED, before any fix, at the FINAL item count:

```
1 failed, 20 passed in 3.14s   tests/production/test_error_signal_ratchet.py
4 failed, 5 passed in 1.19s    tests/test_codegen_build_loop_server.py
```

GREEN, final:

```
21 passed in 3.08s     tests/production/test_error_signal_ratchet.py
72 passed in 1.41s     the three grounded-outcomes files
83 passed in 2.23s     tests/test_agent_tools.py
510 passed in 14.86s   (14 named files, at the Item 2 commit)
634 passed in 25.51s   (17 named files, final)
```

Regression files named deliberately (no selector on this lineage):
`tests/production/test_error_signal_ratchet test_codegen_build_loop_server
test_learning_tools test_agent_tools test_grounded_outcomes
test_grounded_outcomes_infrastructure test_grounded_outcomes_agent_dispatch
test_harness_root_confinement test_agent_dispatch_dev_tools
test_read_only_agent_policy test_learning_health test_calibration
test_end_report_standing test_agent_verification_gate test_activity_verdict
test_outcome_source test_memory_store`

The full suite (~500s) was **not** run. Nothing here changes the storage
schema; the production edits are confined to `server._drain_deferred_distillations`
and its two report call sites, `server._agent_dispatch`'s refusal gate, and
`grounded_outcomes.rendered_infrastructure_error`, and every test file
referencing any of them is in the named set above.

## Commits

```
1ab0038  Give the rootless-dev-tool refusal a gate that owns it (cherry-picked from 8d8c289)
818a5e6  Read the refusal as the header it is, not as the server's wire marker (#63)
aa49197  A raising item is counted, never silently skipped (#64)
de4be5d  The sink doubles guarded eleven negative assertions and could not raise (#64)
```

## NEW findings

**Critical — the ratchet counts syntax, not signals, and the repo documents the
escape.** Measured with the checker's own `_literal_error_prefix` applied to
assignments instead of returns: **245** `ERROR:` returns and **12**
`startswith` parsers are visible to it, while **10** `ERROR:`-prefixed
*assignments* and **1** `"ERROR:" in text` membership test (`server.py:14560`)
have identical semantics and are invisible. Two of those ten are the gate
helpers themselves (`server.py:14734`, `14782`) — including the one this branch
just migrated into. The assign-then-return form is documented in-repo as
acceptable for this checker (`_agent_permission_gate_error`), so a site can
leave the ratchet's universe by being *rewritten* rather than removed, and the
baseline is not a count of stringly `ERROR:` signals. This does not undo the
migration — consolidating three refusals behind one gate is a real structural
gain, and the plant proves a genuine re-addition is still caught — but the
number the ratchet reports should not be read as progress on the underlying
protocol. Closing it means extending the checker to assignment and membership
forms, which will surface those 11 sites and needs its own lane.

**Important — `secret_scan` reports a floor as a total, with `ok: True`.**
`harness_tools.py:994`: `scanned += 1` happens *before* the read, and an
`OSError` on that read is silently skipped. A run where every file was
unreadable returns `{"ok": True, "findings": [], "files_scanned": N,
"truncated": False}` — indistinguishable from a clean scan, on the very tool
`8d8c289` measured leaking a secret. It needs an `unreadable` count in the
result, and `scanned` incremented after the read succeeds.

**Important — `nightly_selfmod.reclaim_orphans` over-counts in the other
direction.** `scripts/nightly_selfmod.py:413`: `selfmod.cancel(rid)` is wrapped
in `except Exception: pass` and `reclaimed += 1` runs unconditionally, so a run
that failed to cancel is still logged as reclaimed. Same shape as the drain,
inverted: a ceiling reported as a total.

**Important — `server._feed_grounded_outcome`'s `except Exception: pass` makes
attribution failures invisible in production too, not only in tests.** The
sinks are fixed so a *double* can no longer be the cause, but a real failure in
`grounded_outcomes.attribute` or in `_record_outcome_signal` is still absorbed
with no counter and no log. Deliberately left: absorbing it is the stated
"bookkeeping must never break the run it is observing" contract, and making it
observable is a change with its own blast radius.

**Note, not a finding — the brief's Item 2 premise was stale.** The three named
doubles were fixed by `4f43bd7`, which is on this branch's own history. The #62
report listed them as open NEW findings in the same commit that closed them.
Worth a fleet-level habit: a report's findings section should be reconciled
against its own diff before it ships, or the next lane spends its budget
re-deriving what was already done.
