# WP1 Three-Hundred-Thirty-Second Slice — session turn claims

## Boundary

The database-backed session turn claims (`_acquire_persistent_session_turn`,
`_release_persistent_session_turn`) now live in
`sonder_runtime/adapters/session_turn_claims.py` as `acquire_session_turn`
and `release_session_turn_claim`, with the owner-identity probe, the bounded
claim wait, the refusal messages and the release retry-then-abandon sequence
unchanged. They open the memory database and probe process liveness through
the packaged adapters, so the adapters layer is their home. The database
opener and the environment-derived claim wait are injected; `server.py`
keeps both root names as thin delegates passing `_open_db` and
`_SESSION_TURN_CLAIM_WAIT_SECONDS` at call time, so the database seam keeps
working.

## Evidence

- `tests/test_session_turn_claims_boundary.py` verifies a successful claim with the owner identity, contended and unavailable coordination refusals without a leaked connection, release with retry and abandonment, and the root delegates' database seam.
- `python -m pytest -q tests/test_session_turn_claims_boundary.py tests/test_server_helpers.py -k 'boundary or session or claim or turn'`
- `python scripts/check_architecture.py`
- `python scripts/check_error_signals.py`
- `python -m compileall -q sonder_runtime server.py`
- `git diff --check`
