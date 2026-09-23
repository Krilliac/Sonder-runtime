# Fanout artifact readiness barrier

`master_orchestrator.run_delegated` treats child output as an artifact at the
fan-in boundary. Before provenance aggregation or the audit prompt can see the
children, each producer must have a `sonder.artifact-readiness.v1` record for
the current master run. The record carries producer and run identity, a schema
version, a completion marker, a SHA-256 content digest, a passed validation
result, and a timezone-aware timestamp. The barrier requires the complete
expected producer set, rejects duplicate or missing producers, stale/future
timestamps, cross-run records, failed validation, and content digest changes.

A deterministic verifier may be supplied for artifact types with a stable
checker. Its identity must be recorded in the readiness record and a rejection
fails the join. The barrier is bounded by the expected producer count and does
not fetch, persist, or execute artifact content.

If a worker fails before publishing readiness, the fan-in returns an error and
the audit lane is not started. This preserves the existing bounded worker
behavior while preventing partial output from becoming aggregate evidence.

Verification:

```text
D:\Sonder-runtime\venv\Scripts\python.exe -m pytest -q tests/test_artifact_readiness.py tests/test_master_orchestrator.py
D:\Sonder-runtime\venv\Scripts\python.exe scripts/check_architecture.py
```
