# Reproducible evaluation evidence — 2026-08-22

This slice extends the existing provider-neutral evaluation domain instead of
adding another standalone benchmark implementation.

## Contracts

- `sonder_runtime/application/evaluation/reproducible.py` owns bounded scenario,
  registry, provider identity, case outcome, matrix, diagnostic, and regression
  contracts. Scenario and report identities use canonical JSON plus SHA-256.
- `TrajectoryRecord` remains the replay format. Its new `from_dict` path checks
  the record digest and every input/output/state digest before accepting stored
  evidence.
- `EvaluationRunReport.as_evaluation_result` projects a run into the existing
  suite/result/proposal lifecycle rather than inventing a parallel promotion
  record.
- `sonder_runtime/adapters/reproducible_evaluation.py` loads bounded,
  digest-checked JSON fixtures, provides a side-effect-free local provider, and
  persists raw replay reports atomically only at an explicit path.
- `scripts/run_reproducible_eval.py` runs one scenario across one or more exact
  provider/model identities. It does not discover models, call a network, or
  promote anything.

## End-to-end evidence

`tests/fixtures/evaluation/scenario.local-tools.v1.json` and
`provider.local-reference.v1.json` are a checked-in public golden pair. The
focused integration test loads both files, registers the scenario, executes
all cases, repeats the run byte-for-byte, saves and reloads the report, replays
the trace, and projects the result into the existing lifecycle type.

Additional tests cover immutable registry versions, deterministic matrix
ordering, duplicate targets, timeout/provider/protocol/invalid-response
taxonomy, case swaps hidden by aggregate pass rate, fixture and report
tampering, provider-identity drift during replay, and CLI input/output collision
protection.

## Privacy and scope

Diagnostic projections contain IDs, counts, digests, and stable reason codes,
not raw requests or outputs. Replay files necessarily contain both and are
written only when a caller supplies a destination. The deterministic fixture
provider proves harness behavior, not model capability. Production provider
adapters and live-model statistical confidence remain outside this offline
slice.
