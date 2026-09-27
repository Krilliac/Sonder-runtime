"""Write app/test/fixtures/server/* with the server's own json.dumps framing."""
import json, os, sys

out = sys.argv[1]
os.makedirs(out, exist_ok=True)


def envelope(code, message, correlation_id, *, retryable):
    # Mirrors sonder_runtime/adapters/web/lifecycle.py:error_envelope.
    legacy_type = {
        "CAPACITY_EXHAUSTED": "rate_limit_error",
        "OWNER_CAPACITY_EXHAUSTED": "rate_limit_error",
        "ADMISSION_TIMEOUT": "server_error",
        "MAINTENANCE_MODE": "server_error",
        "DRAINING": "server_error",
        "AUTH_RATE_LIMITED": "rate_limit_error",
        "WORK_CAPACITY_EXHAUSTED": "rate_limit_error",
        "UNAUTHENTICATED": "auth",
    }.get(code, "server_error")
    return {"error": {"code": code, "message": message,
                      "correlation_id": correlation_id,
                      "retryable": retryable, "type": legacy_type}}


def idem(message, code, status, retryable):
    # Mirrors serve.py:_idempotency_refusal_payload.
    return {"error": {
        "message": message,
        "type": "rate_limit_error" if status == 429 else (
            "server_error" if status >= 500 else "invalid_request"),
        "code": code,
        "retryable": retryable,
    }}


def write(name, obj):
    text = obj if isinstance(obj, str) else json.dumps(obj)
    with open(os.path.join(out, name), "w", newline="") as f:
        f.write(text)


run_id = "wr-7c1e9a4b2d6f40c8a3e5b1d7f9c2e4a6"
assert len(run_id) == 35

# --- captured verbatim from scratchpad/app/parity/*.out ----------------------
write("host_not_allowed_421.json", '{"error": {"message": "host is not allowed for this listener", "type": "invalid_request", "code": "HOST_NOT_ALLOWED"}}')
write("register_bootstrap_403.json", '{"ok": false, "message": "first-admin bootstrap is not authorized"}')
write("register_created_201.json", '{"ok": true, "account": {"username": "parityuser", "role": "admin", "tier": "free", "dev_flags": "", "banned": false, "created_ts": 1790334983, "last_login_ts": null}}')
write("idempotency_key_reused_422.json", '{"error": {"message": "idempotency key reused: this Idempotency-Key already names a different request, so this one was not started. Use a new Idempotency-Key for a new action.", "type": "invalid_request", "code": "IDEMPOTENCY_KEY_REUSED", "retryable": false}}')
write("work_runs_empty.json", '{"runs": []}')

# --- rebuilt from the server source at the recorded commit -------------------
write("auth_rate_limited_429.json", envelope(
    "AUTH_RATE_LIMITED", "too many failed authentication attempts; retry later",
    "req_1f0c2b7e9a4d4c1b8e3f6a5d2c1b0a99", retryable=True))
write("permission_mode_forbidden_403.json", envelope(
    "FORBIDDEN", "administrator authorization is required to change permission mode",
    "req_0a7d3c9e5b1f4e2a8c6d4b2f0e8a6c41", retryable=False))
write("permission_mode_unknown_400.json", {
    "error": "unknown mode 'bogus'. modes: plan, manual, acceptEdits, auto",
    "modes": ["plan", "manual", "acceptEdits", "auto"]})
write("idempotent_action_completed_409.json", idem(
    "idempotent action refused: it already completed before the current server "
    "process. It was not run again; query its status or submit a new action "
    "with a new Idempotency-Key.", "IDEMPOTENT_ACTION_COMPLETED", 409, False))
write("idempotency_receipt_unavailable_503.json", idem(
    "idempotency receipt unavailable: the action was not started. Retry after "
    "restoring local runtime storage.", "IDEMPOTENCY_RECEIPT_UNAVAILABLE", 503, True))
write("work_capacity_exhausted_429.json", envelope(
    "WORK_CAPACITY_EXHAUSTED",
    "routed work capacity is busy (2 of 2 runs active); retry later, or cancel a "
    "run with POST /v1/work-runs/<id>/cancel",
    "req_5e2b8d4a1c7f4a3e9b6d2c8f0a4e6b13", retryable=True))
write("work_runs_forbidden_403.json", {"error": {
    "message": "developer or admin authentication is required for work runs",
    "type": "forbidden", "code": "FORBIDDEN"}})
write("internal_500.txt", "Internal Server Error")

pending_text = (
    "Work is still running as work run %s (wall-clock budget %d s). "
    "Fetch the answer with GET /v1/work-runs/%s, or stop further changes "
    "with POST /v1/work-runs/%s/cancel." % (run_id, 1800, run_id, run_id))
write("chat_work_running.json", {
    "id": "chatcmpl-3b9d2f71a0c4", "object": "chat.completion",
    "created": 1790335000, "model": "sonder",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": pending_text},
                 "finish_reason": "stop"}],
    "sonder_elapsed_ms": 240013,
    "sonder_receipt": {"request_id": "req_9d8c7b6a5f4e4d3c2b1a0f9e8d7c6b5a",
                       "model": "sonder", "tier": "code", "elapsed_ms": 240013,
                       "chat_work": {"status": "running", "work_run_id": run_id,
                                     "session_ref": "parity-thread-1"}},
})
write("work_run_running.json", {
    "id": run_id, "status": "running", "created_at": 1790335000.25,
    "updated_at": 1790335000.25, "deadline_at": 1790336800.25,
    "cancel_requested": False, "output": "", "output_truncated": False})
write("work_run_returned.json", {
    "id": run_id, "status": "returned", "created_at": 1790335000.25,
    "updated_at": 1790335312.5, "deadline_at": 1790336800.25,
    "cancel_requested": False,
    "output": "The PSO cache now warms on load; 3 files changed.",
    "output_truncated": False})
write("work_run_cancelled.json", {
    "id": run_id, "status": "running", "created_at": 1790335000.25,
    "updated_at": 1790335100.0, "deadline_at": 1790336800.25,
    "cancel_requested": True, "output": "", "output_truncated": False})


def chunk(iid, model, delta, finish_reason=None, elapsed_ms=None, receipt=None,
          usage=None, activity=None):
    # Mirrors serve.py:_chunk (created pinned for a stable fixture).
    obj = {"id": "chatcmpl-%s" % iid, "object": "chat.completion.chunk",
           "created": 1790335000, "model": model,
           "choices": ([] if usage is not None and not delta and finish_reason is None
                       else [{"index": 0, "delta": delta, "finish_reason": finish_reason}])}
    if elapsed_ms is not None:
        obj["sonder_elapsed_ms"] = max(0, int(elapsed_ms))
    if receipt:
        obj["sonder_receipt"] = receipt
    if usage is not None:
        obj["usage"] = usage
    if activity is not None:
        obj["sonder_activity"] = activity
    return "data: %s\n\n" % json.dumps(obj)


KEEP = ": keep-alive\n\n"
iid = "a1b2c3d4e5f6"
receipt = {"request_id": "req_c4303a90110e42eb967f40b997a7c088", "model": "sonder",
           "tier": "chat", "elapsed_ms": 136293}
activity = {"id": "r000004", "label": "chat:sonder", "surface": "http",
            "model": "sonder", "status": "complete", "elapsed_ms": 136250,
            "tool_calls": 0, "model_calls": 1}
usage = {"prompt_tokens": 2600, "completion_tokens": 143, "total_tokens": 2743}
write("stream_ok.sse",
      KEEP + KEEP
      + chunk(iid, "sonder", {"role": "assistant", "content": "PELI"})
      + chunk(iid, "sonder", {"content": "CAN"})
      + chunk(iid, "sonder", {}, finish_reason="stop", elapsed_ms=136293,
              receipt=receipt, activity=activity)
      + chunk(iid, "sonder", {}, usage=usage)
      + "data: [DONE]\n\n")
write("stream_keepalive_only.sse", KEEP * 3)
error_frame = {"id": "chatcmpl-%s" % iid, "object": "error", "model": "sonder",
               "error": {"message": "the model backend failed while generating",
                         "type": "server_error", "code": "MODEL_BACKEND_ERROR"}}
write("stream_error.sse",
      KEEP + chunk(iid, "sonder", {"role": "assistant", "content": "partial "})
      + "data: %s\n\n" % json.dumps(error_frame) + "data: [DONE]\n\n")
write("stream_dropped.sse",
      KEEP + chunk(iid, "sonder", {"role": "assistant", "content": "half an ans"}))
