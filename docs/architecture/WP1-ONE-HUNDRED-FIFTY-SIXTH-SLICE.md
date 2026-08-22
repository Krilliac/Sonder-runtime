# WP1 One-Hundred-Fifty-Sixth Slice — environment probe platform ownership

## Boundary

Moved deterministic host-environment discovery from the root
`environment_probe.py` module into the canonical
`sonder_runtime.platform.environment_probe` boundary. The root module remains
an identity-preserving compatibility import, so existing callers share the
packaged cache and monkeypatch surface while platform ownership is explicit.
The probe remains read-only and does not spawn processes or collect versions.

## Verification

- `python -m pytest tests/test_environment_probe.py tests/test_environment_probe_ownership.py tests/test_toolchain_status.py -q` — pass.
- `python scripts/check_architecture.py` — pass.
- `python scripts/check_requirement_evidence.py` — pass.
- `python -m compileall -q sonder_runtime environment_probe.py toolchain_status.py` — pass.
- `git diff --check` — pass.
