# WP1 Forty-Second Slice: Distillation Reporting Formatter Boundary

Status: implemented on `agent/wp1-execution-status`.

## Scope

Moved the deterministic `_drain_backlog_text` and `_drain_summary_text`
presentation helpers from `server.py` into the canonical
`sonder_runtime.adapters.observability.distillation_formatting` adapter.
`server.py` retains the same imported names, preserving its existing reporting
and test-facing contract while removing formatting implementation from the
composition root.

No command catalog, persistence, launcher, or strangler-services files were
changed by this slice.

## Evidence

- Focused formatter and codegen regression tests pass.
- `python -m compileall -q sonder_runtime server.py` passes.
- `scripts/check_architecture.py` passes.
- `scripts/check_requirement_evidence.py` passes.
- `git diff --check` and `git diff --cached --check` pass.

## Remaining boundary

The root `server.py` composition boundary and the remaining compatibility
paths require additional bounded migrations.
