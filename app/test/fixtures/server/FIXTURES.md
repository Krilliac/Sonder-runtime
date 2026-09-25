# Server response fixtures

Recorded against server commit `5f8c7665` (Sonder Runtime). When the server's
wording or envelope changes, update the fixture in the same change so the
difference is visible in review.

Captured verbatim from the live parity run (`scratchpad/app/parity/*.out`):

| File | Route | Status |
|---|---|---|
| `host_not_allowed_421.json` | any, Host not in `allowed_hosts` | 421 |
| `register_bootstrap_403.json` | `POST /v1/sonder/register` without `X-Sonder-Bootstrap-Secret` | 403 |
| `register_created_201.json` | `POST /v1/sonder/register` with the secret | 201 |
| `idempotency_key_reused_422.json` | `POST /v1/permission-mode`, same key, different mode | 422 |
| `work_runs_empty.json` | `GET /v1/work-runs` | 200 |

Rebuilt with the server's own framing (`json.dumps` of the same dicts) from the
source at that commit, because the live run did not trigger them:

| File | Source |
|---|---|
| `auth_rate_limited_429.json` | `serve.py:_auth_rate_limited` + `lifecycle.error_envelope` (`Retry-After: 2`) |
| `permission_mode_forbidden_403.json` | `serve.py` permission-mode POST authority check |
| `permission_mode_unknown_400.json` | `serve.py:_handle_permission_mode_post` |
| `idempotent_action_completed_409.json`, `idempotency_receipt_unavailable_503.json` | `serve.py:_idempotency_refusal_payload` |
| `work_capacity_exhausted_429.json` | `serve.py` `WorkCapacityExhausted` -> `AdmissionRejected` |
| `work_runs_forbidden_403.json` | `serve.py:_handle_work_run_request` |
| `work_run_*.json` | `adapters/persistence/http_work_runs.py:_public` |
| `chat_work_running.json` | `serve.py:_work_run_pending_text` + `handoff_receipts.public_receipt` |
| `stream_*.sse` | `serve.py:_chunk`, `_write_stream_body`, `_send_stream_terminal_error`, `sse.KEEPALIVE_FRAME` |
| `internal_500.txt` | a proxy's non-JSON 500 |

Regenerate the rebuilt ones with `python3 test/fixtures/server/generate_fixtures.py test/fixtures/server` from `app/`.
