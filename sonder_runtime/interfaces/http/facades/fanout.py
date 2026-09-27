"""HTTP model-fanout routes: run history, one receipt, and its mutations.

* ``GET /v1/fanout`` lists recent runs (developer or admin authority; a
  non-admin account sees only its own runs, keyed by its opaque owner);
* ``GET /v1/fanout/<run_id>`` returns one caller-authorized receipt;
* ``POST /v1/fanout/<run_id>/cancel``, ``resume`` and ``synthesize`` mutate
  it, each bound to an opt-in ``Idempotency-Key`` replay guard over the run
  and the complete action payload; synthesis also spends the caller's
  inference rate-limit budget.

``handler`` is serve.py's request handler: these functions use its auth
check, its per-run owner check (``_fanout_run_for_context``), its headers and
its JSON sender, so the wire behaviour is the handler's. The legacy runtime
namespace (fanout store, receipt and synthesis operations), the account
rate limiter, the replay guard and the query parser are injected by
``serve.py``, which keeps this module free of adapters and transport.
"""
from __future__ import annotations

from typing import Any, Callable


def serve_get(handler: Any, route: str, *, parse_query: Callable[[], dict],
              developer_authorized: Callable[[Any], bool],
              request_owner_of: Callable[[Any], str], runtime: Any) -> bool:
    """``GET /v1/fanout`` (run history) and ``GET /v1/fanout/<run_id>``.

    ``route`` is the request path without a trailing slash; ``parse_query``
    parses the request's query string. Returns ``False`` for another route.
    """
    if route == "/v1/fanout":
        context = handler._request_auth_context()
        if not context["authorized"]:
            handler._send_auth_error()
            return True
        if not developer_authorized(context):
            handler._send_json_payload({"error": {"message": "developer or admin authentication is required for model fanout", "type": "forbidden"}}, status=403)
            return True
        query = parse_query()
        # A history query is a small, explicitly bounded contract.  Do
        # not let duplicate values acquire accidental first-value-wins
        # semantics through a proxy or a client encoder.
        for name in ("limit", "include_finished"):
            if len(query.get(name, ())) > 1:
                handler._send_json_payload({"error": {"message": "%s must be supplied at most once" % name, "type": "invalid_request"}}, status=400)
                return True
        limit_text = (query.get("limit") or ["20"])[0]
        finished_text = (query.get("include_finished") or ["true"])[0].casefold()
        try:
            limit = int(limit_text)
        except (TypeError, ValueError):
            handler._send_json_payload({"error": {"message": "limit must be an integer between 1 and 100", "type": "invalid_request"}}, status=400)
            return True
        if not 1 <= limit <= 100:
            handler._send_json_payload({"error": {"message": "limit must be an integer between 1 and 100", "type": "invalid_request"}}, status=400)
            return True
        if finished_text not in ("true", "false"):
            handler._send_json_payload({"error": {"message": "include_finished must be true or false", "type": "invalid_request"}}, status=400)
            return True
        account = context.get("account") or {}
        request_owner = None
        if context.get("mode") != "local-open" and account.get("role") != "admin":
            request_owner = request_owner_of(context)
        handler._send_json_payload({"runs": runtime.fanout_store.recent_run_summaries(
            request_owner=request_owner, include_finished=finished_text == "true",
            limit=limit,
        )})
        return True
    prefix = "/v1/fanout/"
    if not route.startswith(prefix) or "/" in route[len(prefix):]:
        return False
    run_id = route[len(prefix):]
    if not run_id or len(run_id) > 80:
        handler._send_json_payload({"error": {"message": "invalid fanout run id", "type": "invalid_request"}}, status=400)
        return True
    context = handler._request_auth_context()
    if not context["authorized"]:
        handler._send_auth_error()
        return True
    _run, error = handler._fanout_run_for_context(context, run_id)
    if error:
        status, message = error
        handler._send_json_payload({"error": {"message": message, "type": "forbidden" if status == 403 else "not_found"}}, status=status)
        return True
    handler._send_json_payload(runtime._fanout_receipt(run_id))
    return True


def serve_post(handler: Any, path: str, req: Any, context: Any, *, runtime: Any,
               account_auth: Any, idempotent_http_action: Callable[..., Any],
               send_idempotency_refusal: Callable[[Any, Any], bool]) -> bool:
    """Mutate or locally synthesize one caller-authorized fanout receipt.

    ``POST /v1/fanout/<run_id>/cancel|resume|synthesize``. Returns ``False``
    for another route.
    """
    prefix = "/v1/fanout/"
    if not path.startswith(prefix):
        return False
    suffix = path[len(prefix):].strip("/")
    parts = suffix.split("/")
    if (len(parts) != 2 or parts[1] not in ("cancel", "resume", "synthesize")
            or not parts[0] or len(parts[0]) > 80):
        return False
    run_id, action = parts
    if not context["authorized"]:
        handler._send_auth_error()
        return True
    _run, error = handler._fanout_run_for_context(context, run_id)
    if error:
        status, message = error
        handler._send_json_payload({"error": {"message": message, "type": "forbidden" if status == 403 else "not_found"}}, status=status)
        return True
    supplied_key = handler.headers.get("Idempotency-Key", "")

    def replay(action_name, factory):
        # The run is already owner-authorized above.  Bind each replay to
        # both that durable run and the complete small action payload, so
        # a client key cannot turn a cancel into a resume or select a
        # different synthesis model.  _http_action_idempotency_key hashes
        # this text; neither it nor the raw header is retained.
        return idempotent_http_action(
            context,
            supplied_key,
            "fanout\0%s\0%s" % (run_id, action_name),
            factory,
        )
    if action == "synthesize":
        if set(req) - {"synth_model"}:
            handler._send_json_payload({"error": {"message": "synthesis accepts only synth_model", "type": "invalid_request"}}, status=400)
            return True
        synth_model = req.get("synth_model", "")
        if not isinstance(synth_model, str):
            handler._send_json_payload({"error": {"message": "synth_model must be a string", "type": "invalid_request"}}, status=400)
            return True
        # Synthesis starts a fresh bounded local generation.  In shared
        # deployments it must consume the same per-account admission
        # budget as chat completions, otherwise callers can bypass the
        # inference rate limit by repeatedly synthesizing one receipt.
        conn = runtime._open_db()
        try:
            ok, message = account_auth.rate_limit(conn, context.get("account"))
        finally:
            conn.close()
        if not ok:
            handler._send_json_payload(
                {"error": {"message": message, "type": "rate_limit"}},
                status=429,
            )
            return True
        try:
            payload = replay(
                "synthesize\0%s" % synth_model,
                lambda: runtime._fanout_synthesize_run(_run, synth_model),
            )
            if not send_idempotency_refusal(handler, payload):
                handler._send_json_payload(payload)
        except runtime.ModelCallError as exc:
            status = exc.status or (
                400 if exc.kind == "configuration" else
                504 if exc.kind == "timeout" else 502
            )
            if status == 408:
                status = 504
            if status not in (400, 403, 404, 429, 502, 503, 504):
                status = 502
            error_type = "invalid_request_error" if 400 <= status < 500 else "server_error"
            headers = None
            if status in (429, 503, 504):
                wait = exc.retry_after_seconds
                retry_after = 1 if wait is None else max(0, int(round(wait)))
                headers = {"Retry-After": str(retry_after)}
            handler._send_json_payload(
                {"error": {"message": exc.detail, "type": error_type}},
                status=status, headers=headers,
            )
        return True
    if action == "cancel":
        cancelled = replay("cancel", lambda: runtime.fanout_store.request_cancel(run_id))
        if send_idempotency_refusal(handler, cancelled):
            return True
    else:
        for name in ("include_failed", "retry_unknown"):
            if name in req and not isinstance(req[name], bool):
                handler._send_json_payload({"error": {"message": "%s must be a boolean" % name, "type": "invalid_request"}}, status=400)
                return True
        include_failed = req.get("include_failed") is True
        retry_unknown = req.get("retry_unknown") is True

        def resume():
            resumed = runtime.fanout_store.resume_run(
                run_id,
                include_failed=include_failed,
                retry_unknown=retry_unknown,
            )
            if resumed is None:
                return False
            # A resume is an explicit replay instruction. _execute
            # preserves the stored snapshot and never retries unknown rows
            # unless this request included retry_unknown=true.
            runtime._execute_fanout_run(run_id)
            return True

        resumed = replay(
            "resume\0include_failed=%d\0retry_unknown=%d" % (
                include_failed, retry_unknown,
            ),
            resume,
        )
        if send_idempotency_refusal(handler, resumed):
            return True
        if resumed is None:
            handler._send_json_payload({"error": {"message": "fanout run is not resumable with the selected retry options", "type": "invalid_request"}}, status=400)
            return True
        if not resumed:
            handler._send_json_payload({"error": {"message": "fanout run is not resumable with the selected retry options", "type": "invalid_request"}}, status=400)
            return True
    receipt = runtime._fanout_receipt(run_id)
    handler._send_json_payload(receipt or {"error": {"message": "fanout receipt was unavailable", "type": "not_found"}}, status=200 if receipt else 404)
    return True


__all__ = ["serve_get", "serve_post"]
