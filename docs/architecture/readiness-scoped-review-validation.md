# Readiness and scoped-review verification

Verified locally on 2026-09-29 against base `45a3e093aa423c3a23f9e356419972d2332015e5`.
Branch: `feat/readiness-fanin-scoped-review`.

## Measured scope

| Surface | Before | After |
| --- | --- | --- |
| Live registered MCP tools, loaded from original/new server in separate isolated processes | 223 | 223; zero schema, description or risk changes |
| Risk distribution | safe 86, ask 63, dangerous 21, execution 30, mutation 23 | identical |
| Root decorated tool signatures/docstrings | 216 | 216; zero changes |
| Native typed catalog tools | 66 | 66; all seven committed catalog files pass freshness checks |
| Built-in role defaults | 0/6 SCOPED | 2/6 SCOPED: verifier and reviewer |
| Generic critic adapter | UNSPECIFIED | SCOPED; explicit policy overrides win |
| Model-fanout synthesis entrances | MCP and HTTP | 2/2 use the validated source builder |
| Fleet/master synthesis boundaries | one shared delegated join | 1/1 validates every expected slot |
| Parallel generate tools | two | 2/2 validate before selecting winners |
| server.py lines / cap | 26931 / 26942 | 26899 / 26942 |
| master_orchestrator.py lines / cap | 3026 / 3026 | 3024 / 3026 |

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

The generator factory has 27 production call sites (including evaluation and
curriculum entrypoints). Its signature, return text and public metadata remain
unchanged. Only the two parallel tools install its thread-local completion
observer. Existing generator regressions and a concurrent metadata regression
cover that seam. No additional model calls, probes, retries or waits were added.

## Parity and latency

The original fanout, parallel-Python, parallel-language and master functions
were extracted from the base commit and executed against the same collaborators
as the changed functions. Complete-valid synthesis strings, returned provenance
and output matched exactly. Master was compared with one and three workers,
fixed run IDs and deterministic completion order. Permanent tests retain exact
fanout/parallel output assertions and master prompt/receipt canaries.

For eight 8 KB fanout answers over 200 iterations, one local CPU measurement
was 0.185 ms before and 0.317 ms after (about 0.132 ms added for manifest parsing,
hashing and validation). This is a local microbenchmark, not a service latency
SLA. Readiness does not add network or verifier executions. Master fan-in
still recomputes each child's objective-coverage receipt from the rendered
output exactly as the base revision did; an intermediate revision of this
change trusted the producer's own receipt copy and was reverted to the base
behaviour before commit.

## Regression coverage

Added or expanded tests exercise complete-valid parity, stored-byte tampering,
storage/provider truncation, still-writing/missing slots, stale/cross-run
evidence, configured-verifier receipt requirements and independent receipt
matching. Candidate tests prove rejected code cannot become a winner and that
two concurrent generator calls cannot exchange completion metadata.

Role tests cover all six built-in defaults, the critic adapter, explicit CLEAN
and INHERIT, preservation of existing execution constraints and registry gates,
and actual editor-to-verifier-to-reviewer dispatch. The latter proves that the
editor diff and explicit test evidence survive while rationale is withheld.

The final 24-file regression run collected 829 cases: **827 passed, 1 skipped,
1 failed**. The failure was the existing two-second background-worker startup
wait in `test_cancel_master_skips_queued_workers_and_discards_running_result`.
The unchanged base implementation also failed that test and the five-second
concurrency startup wait in a separate comparison process; both cases passed
against the changed implementation when run together afterwards (2 passed).
The timing sensitivity is retained as a qualification limit, not hidden by
the passing rerun.

Other completed checks: 154 combined readiness/master/Fleet/context cases;
302 worker/continuation/server-helper cases (1 Windows skip); 71 role/SDK and
baseline-parity cases; 6 final master completion/receipt canaries, including
the subsequently added missing-worker-slot regression. Runs overlap and are
not summed. There are 24 new test functions plus updated legacy canaries.

## Gates and full-suite parity

All five repository gates exit 0 with `D:/sonder-eco/venv-rt/Scripts/python.exe`:
`check_lint_ratchet.py`, `check_architecture.py`, `check_doc_links.py`,
`check_documentation_authority.py` and `generate_documentation_catalogs.py --check`.
The permission risk sweep reports 339 catalog commands on both origin/main and
this branch with 0 added, removed or changed risk levels.

Full-suite parity was run on Node1 with identical arguments against this
branch and the unchanged main checkout; see the pull request for the counts.

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
- `tests/test_artifact_readiness.py`
- `tests/test_master_orchestrator.py`
- `tests/test_model_fanout.py`
- `tests/test_production_artifact_fanin.py`
- `tests/test_remaining_agent_010.py`
- `tests/test_scoped_reviewer_context.py`
