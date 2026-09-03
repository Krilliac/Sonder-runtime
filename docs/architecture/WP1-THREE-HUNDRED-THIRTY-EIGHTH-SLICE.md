# WP1 Three-Hundred-Thirty-Eighth Slice — fanout worker identity

## Boundary

Moved `_FANOUT_WORKER_INSTANCE` (per-process UUID token) into `sonder_runtime/domain/fanout_worker_identity.py` as `FANOUT_WORKER_INSTANCE`, alongside a pure `fanout_worker_id(instance, pid, thread_ident)` formatter. The root `_FANOUT_WORKER_INSTANCE` is an identity-preserving alias; `_fanout_worker_id` is a compatibility delegate that reads `_FANOUT_WORKER_INSTANCE`, `os.getpid()`, and `threading.get_ident()` from server's namespace so that existing monkeypatch surfaces in `test_fanout_store` survive unchanged.

## Evidence

- `tests/test_fanout_worker_identity_boundary.py` verifies constant alias identity, delegate equivalence, format, stability, hex validation, and monkeypatch surfaces.
- `python scripts/check_architecture.py`
- `python scripts/check_error_signals.py`
- `python -m compileall -q sonder_runtime tests server.py`
- `git diff --check`
