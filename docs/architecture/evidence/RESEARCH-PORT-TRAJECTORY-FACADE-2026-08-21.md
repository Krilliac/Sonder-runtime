# Research port: trajectory facade integration

Date: 2026-08-21  
Branch: `agent/port-research-findings`

The trajectory projection introduced in `df6d1336` is now reachable through
the existing typed session HTTP facade at
`/v1/sessions/{session_id}/trajectory`.

The route consumes the redacted replay export, rejects truncated histories, and
returns the deterministic `sonder.session-trajectory.v1` envelope. It does not
add a second event store or bypass session integrity checks.

Evidence: `tests/test_http_session_integration.py`.
