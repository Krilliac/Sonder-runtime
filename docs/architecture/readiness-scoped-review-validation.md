# Readiness and scoped-review verification

First verified on 2026-09-29 against base `45a3e093aa423c3a23f9e356419972d2332015e5`.
Re-verified on 2026-10-01 after merging `origin/main` `dc6066101c148405742f0784d17b5f8ffc3c7012`
into `feat/readiness-fanin-scoped-review` (branch commit `515328ac`). The table
and the full-suite parity compare that main with that commit. Measurements that
were only taken on the original base say so.

A later merge of main `f085be48` (#619) touched autopilot code only. On that merge
the gates, the risk sweep (344, 0 changes), the tool-surface comparison (216,
0 changes) and the focused suites were re-run, with #619's three autopilot test
files added. `server.py` is then 26915 lines on main and 26883 on this branch.

## Measured scope

| Surface | main `dc606610` | This branch |
| --- | --- | --- |
| `@mcp.tool` functions in `server.py` (static AST: decorator, signature, docstring) | 216 | 216; zero changes |
| Permission risk sweep (`risk_dump.py` catalog commands) | 344 | 344; 0 added, removed or changed |
| Committed runtime catalogs (`generate_runtime_catalogs.py --runtime`) | committed | regenerated with no content change |
| Built-in role defaults | 0/6 SCOPED | 2/6 SCOPED: verifier and reviewer |
| Generic critic adapter | UNSPECIFIED | SCOPED; explicit policy overrides win |
| Model-fanout synthesis entrances | MCP and HTTP | 2/2 use the validated source builder |
| Fleet/master synthesis boundaries | one shared delegated join | 1/1 validates every expected slot |
| Parallel generate tools | two | 2/2 validate before selecting winners |
| server.py lines / cap | 26923 / 26942 | 26891 / 26942 |
| master_orchestrator.py lines / cap | 3026 / 3026 | 3017 / 3026 |

On the original base the live registered tool list (223 tools, loaded from the
original and the changed server in separate isolated processes) also showed no
schema, description or risk change.

The module-size caps do not move with this branch. `check_lint_ratchet.py`
lowers a limit only when run with `--update`, and this branch does not run it.

The bounded production caller inventory found two `DelegationRequest`
construction sites, four `WorkerLaunch` construction sites, one `_next_prompt`
caller and one `launch_for` caller. New launch factories apply role defaults;
the SQLite and continuation readers preserve persisted contracts. Canonical
continuation decoding is reused rather than introducing a partial decoder.

Fanout storage has two production `record_result` calls, both in server
execution: the ordinary result and the vault-unavailable failure. Sealing is
conditional on a complete answer, so failure and retry state remain unchanged.
The single synthesis-source caller is shared by the direct MCP route and HTTP
facade. The two parallel functions are registered MCP entrypoints. Fleet enters
the shared delegated join through the master runner; inline mode has no fan-in.

The generator factory's signature, return text and public metadata remain
unchanged. Only the two parallel tools bind its thread-local completion
observer. Since #610 they receive the generator through `_make_tier_generate`,
whose wrapper forwards attribute reads to the raw closure but not writes, so
the observer is bound to the raw closure that records each reply. No model
calls, probes, retries or waits were added.

## Parity and latency

On the original base, the original fanout, parallel-Python, parallel-language
and master functions were extracted from the base commit and executed against
the same collaborators as the changed functions. Complete-valid synthesis
strings, returned provenance and output matched exactly. Master was compared
with one and three workers, fixed run IDs and deterministic completion order.
The permanent tests that keep those guarantees (exact fanout bundle and
provenance, exact parallel output for passing and failing candidates, master
prompt and receipt canaries) pass on the merged branch.

For eight 8 KB fanout answers over 200 iterations, one local CPU measurement on
the original base was 0.185 ms before and 0.317 ms after (about 0.132 ms added
for manifest parsing, hashing and validation). This is a local
microbenchmark, not a service latency SLA. Readiness does not add network or
verifier executions. Master fan-in still recomputes each child's
objective-coverage receipt from the rendered output exactly as the base
revision did.

## Regression coverage

Added or expanded tests exercise complete-valid parity, stored-byte tampering,
storage/provider truncation, still-writing/missing slots, stale/cross-run
evidence, configured-verifier receipt requirements and independent receipt
matching. Candidate tests prove rejected code cannot become a winner, that
already-failed candidates render byte-identically to the base revision, and that
two concurrent generator calls cannot exchange completion metadata.

Two candidate tests were added after merging main, each shown failing before
it passed:

- `test_tier_wrapped_shared_generator_keeps_completion_per_producer` races two
  calls on the generator that `_make_tier_generate` builds. With the observer
  bound to the wrapper, the truncated reply was reported ready. It passes with
  the observer bound to the raw closure.
- `test_bridged_tier_length_reply_never_reaches_verifier_or_winner` runs
  `parallel_generate_run` on a tier bound to sonder-inference. With the
  provider bridge's `done_reason` removed, which is main before #616, the
  truncated reply reached the verifier. It passes on the merged tree.

Role tests cover all six built-in defaults, the critic adapter, explicit CLEAN
and INHERIT, preservation of existing execution constraints and registry gates,
and actual editor-to-verifier-to-reviewer dispatch. The latter proves that the
editor diff and explicit test evidence survive while rationale is withheld.

Focused suites on the merged tree, run on Node1 with `-n 4`:

- 10 files collected 402 cases, and all 402 passed. The files cover
  production fan-in, artifact readiness, scoped context, the master
  orchestrator, model fanout, recursive delegation, agent 010, provider
  routing, the provider bridge and tier-helper boundaries.
- 17 more files collected 339 cases: 337 passed and 2 were skipped. These files
  cover the fanout store and receipts, worker registries, delegation evidence,
  fleet provenance and workflow, tier generation, ensemble answers and the
  codegen build loop. Both skips are platform skips: one needs a POSIX
  case-sensitive filesystem and the other is Linux-only.

The master-cancel startup-timing test that failed intermittently on the
original base passed. Main removed that race in #621.

## Gates and full-suite parity

Every gate exits 0 on the merged tree. `check_lint_ratchet.py` runs under
`D:/sonder-eco/venv-rt/Scripts/python.exe` (ruff 0.16.9, the CI pin; the
repository venv has no ruff, which the gate reports as a tool failure, not a
pass). The others run with the repository venv: `check_architecture.py`,
`check_requirement_evidence.py` (also against the base), `check_error_signals.py`,
`check_history_privacy.py`, `check_doc_links.py`, `check_documentation_authority.py`,
and `generate_documentation_catalogs.py --check` after
`generate_documentation_catalogs.py --write` and
`generate_runtime_catalogs.py --runtime`. The permission risk sweep reports 344
catalog commands with 0 added, removed or changed risk levels against the
2026-10-01 main baseline.

Full-suite parity ran on Node1 with identical arguments
(`pytest -q -p no:cacheprovider -rfE -n 4 tests`), in the same window, against
this branch and against main `dc606610`:

| Run | Failed | Errors | Passed | Skipped | xfailed |
| --- | ---: | ---: | ---: | ---: | ---: |
| main `dc606610` | 195 | 22 | 23472 | 443 | 2 |
| This branch | 195 | 22 | 23515 | 443 | 2 |

The failed and error ids are the same 217 on both sides. None is only on the
branch and none is only on main, and none is in a test file this change adds
or extends. The 43 extra passes are exactly the new cases: 32 in the two new
test files and 11 in the four extended ones (310 collected against main's 299).

Role-by-role prompt examples are in [scoped reviewer context](SCOPED-REVIEWER-CONTEXT.md).

## Files with content changes

- `docs/architecture/SCOPED-REVIEWER-CONTEXT.md`
- `docs/architecture/fanout-artifact-readiness.md`
- `docs/architecture/readiness-scoped-review-validation.md`
- `master_orchestrator.py`
- `server.py`
- `sonder_runtime/adapters/fanout_receipt.py`
- `sonder_runtime/adapters/fanout_synthesis.py`
- `sonder_runtime/adapters/persistence/fanout_store.py`
- `sonder_runtime/application/agents/delegation_service.py`
- `sonder_runtime/application/agents/lineage_delegation.py`
- `sonder_runtime/application/agents/workflow_integration.py`
- `sonder_runtime/application/artifacts/candidates.py`
- `sonder_runtime/application/artifacts/fanin.py`
- `sonder_runtime/application/artifacts/master_fanin.py`
- `sonder_runtime/application/ports/worker_registry.py`
- `sonder_runtime/application/worker_registry/service.py`
- `tests/test_master_orchestrator.py`
- `tests/test_model_fanout.py`
- `tests/test_production_artifact_fanin.py`
- `tests/test_recursive_delegation.py`
- `tests/test_remaining_agent_010.py`
- `tests/test_scoped_reviewer_context.py`

The regenerated `docs/architecture/generated/architecture-map.*` and
`runtime-reference.*` also change: the map counts the four new modules and the
reference digest covers the merged `server.py`.
