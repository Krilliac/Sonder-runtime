# WP1 one-hundred-sixteenth slice — process-probe adapter ownership

## Scope

`LegacyProcessProbe` was the remaining process-liveness implementation in the
generic `strangler_services` module even though its concrete infrastructure
already lived in `process_liveness`. This slice moves the port adapter to
`sonder_runtime.adapters.process_probe.ProcessProbeAdapter` and wires the
composition root directly to that named adapter. The remaining strangler
classes retain their separate repository, model, event, and tool seams.

## Verification

- `pytest -q tests/test_legacy_process_probe.py tests/test_process_liveness.py tests/production/test_composition_root.py` — **43 passed**, one known non-fatal pytest cache-permission warning.
- `python -m compileall -q sonder_runtime/adapters/process_probe.py sonder_runtime/adapters/strangler_services.py sonder_runtime/bootstrap/app.py` — **pass**.
- `python scripts/check_architecture.py` — **pass, zero violations**.
- `python scripts/check_requirement_evidence.py` — **pass**.
- `git diff --check` — **pass**.

This slice does not modify `server.py`, `unsafe_lab.py`, or the security-policy
boundary.
