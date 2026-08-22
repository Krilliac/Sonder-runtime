# WP1 Seventy-Seventh Slice: local observability logging seam

The process-local observability adapter was the next safe packaged caller of
the root `sonder_logging` module. Its `Redactor` and `REDACTION_FAILED`
imports now come from `sonder_runtime.platform.logging`.

The platform module re-exports the exact root objects, so logger identity,
configured handlers, and redaction behavior remain unchanged. Persistence and
the other compatibility callers remain outside this bounded slice.

No server, persistence, command-catalog, launcher, HTTP/REPL, or strangler
service paths were changed.

## Evidence

- `tests/test_logging_platform_seam.py`
- `tests/test_local_observability.py`
- `python -m compileall -q sonder_runtime server.py`
- `python scripts/check_architecture.py`
- `python scripts/check_requirement_evidence.py`
- `git diff --cached --check`
- `git diff --check`
