"""HTTP non-chat POST route handlers (admin, accounts, jobs, compute, A2A, extensions).

Extracted from ``sonder_runtime/interfaces/http/serve.py``.  Imports only
``sonder_runtime.application``, ``sonder_runtime.interfaces``, and non-IO
stdlib modules.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sonder_runtime.application.context import bind_operation_context
from sonder_runtime.application.ports.jobs import JobIdentity
from ..serve_policy import extract_request_temperature
from .a2a_jsonrpc import dispatch_a2a_jsonrpc_route
from .compute_fabric import dispatch_compute_job_cancel, dispatch_compute_job_submit
from .compute_inventory import dispatch_compute_refresh
from .config_wiring import HTTPRequestError
from .extensions import dispatch_extension_route
from .host_tools import dispatch_tool_inventory_refresh
from .model_request import ModelFacadeError, ModelRequestFacade
from .observatory_routes import admit_chat_caller


def handle_admin_drain(
    handler: Any,
    serve_mod: Any,
    *,
    lifecycle_get: Callable[[], Any],
    error_envelope_fn: Callable[..., Any],
    thread_factory: Callable[..., Any],
) -> None:
    """``POST /v1/admin/drain`` — administrator-only, idempotent."""
    if handler._auth_rate_limited():
        return
    context = handler._request_auth_context()
    if not context["authorized"]:
        handler._send_auth_error()
        return
    if not serve_mod._admin_authorized(context):
        handler._send_json_payload(
            error_envelope_fn(
                "FORBIDDEN",
                "administrator authorization is required",
                handler._correlation(),
                retryable=False,
            ),
            status=403,
        )
        return
    lifecycle = lifecycle_get()

    def start_drain() -> dict[str, Any]:
        serve_mod._serve_logger.info(
            "Admin drain requested, initiating graceful shutdown"
        )
        thread_factory(
            target=lifecycle.drain,
            kwargs={"reason": "admin drain request"},
            daemon=True,
            name="sonder-admin-drain",
        ).start()
        return {
            "draining": True,
            "correlation_id": handler._correlation(),
        }

    payload = lifecycle.idempotent(
        serve_mod._request_idempotency_key(
            context, "/v1/admin/drain", handler.headers.get("Idempotency-Key", ""),
        ),
        start_drain,
    )
    handler._send_json_payload(payload, status=202)


def handle_account_logout(
    handler: Any,
    serve_mod: Any,
    *,
    previous_key_valid: Callable[[str], bool],
    lifecycle_get: Callable[[], Any],
) -> None:
    """Revoke an explicitly supplied login; never infer a target account."""
    response_headers = {
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
    }

    def reply(payload: dict[str, Any], status: int = 200) -> None:
        handler._send_json_payload(payload, status=status, headers=response_headers)

    values: Any = handler.headers.get_all("X-Sonder-Account-Token") or ()
    token = serve_mod._bearer_token(values[0]) if len(values) == 1 else ""
    if not isinstance(token, str) or not 1 <= len(token) <= 512:
        reply({"ok": False, "message": "account session required"}, 401)
        return
    mode = serve_mod._effective_auth_mode()
    if mode in ("api-key", "both"):
        if handler._auth_reserve_refused():
            return
        authorization = handler.headers.get("Authorization", "")
        api_key = serve_mod.API_KEY
        if not (
            bool(api_key)
            and (
                serve_mod.check_auth(authorization, api_key)
                or previous_key_valid(serve_mod._bearer_token(authorization))
            )
        ):
            lifecycle_get().record_auth_failure(
                handler._client_ip(), "invalid-credentials", charge=False,
            )
            reply({"ok": False, "message": "authentication required"}, 401)
            return
        lifecycle_get().refund_auth_attempt(handler._client_ip())
    try:
        request = handler._read_json(max_bytes=1024)
    except HTTPRequestError as error:
        reply({"ok": False, "message": error.message}, error.status)
        return
    if request != {}:
        reply({"ok": False, "message": "logout requires an empty object"}, 400)
        return
    conn: Any = None
    try:
        conn = serve_mod.server._open_db()
        serve_mod.admin_auth.revoke_session(conn, token)
    except Exception:
        reply({"ok": False, "message": "session revocation unavailable"}, 503)
        return
    finally:
        if conn is not None:
            conn.close()
    reply({"ok": True})


def handle_admitted_compute_refresh(
    handler: Any,
    serve_mod: Any,
    *,
    default_app_fn: Callable[[], Any],
) -> bool:
    """Admin-only ``POST /v1/compute/nodes/refresh`` handler."""
    if handler._reject_disallowed_origin():
        return True
    if handler._auth_rate_limited():
        return True
    context = handler._request_auth_context()
    if not context["authorized"]:
        handler._send_auth_error()
        return True
    if not serve_mod._admin_authorized(context):
        handler._send_json_payload({"error": {"code": "FORBIDDEN"}}, status=403)
        return True
    try:
        if "?" in handler.path:
            raise ValueError("refresh accepts JSON pagination only")
        payload = handler._read_json(max_bytes=32 * 1024)
        dispatch_fn: Callable[..., tuple[int, Any]] = dispatch_compute_refresh
        status, body = dispatch_fn(default_app_fn().compute_refresh_page, payload)
    except HTTPRequestError as error:
        status, body = error.status, {"error": {"code": "INVALID_REFRESH_REQUEST"}}
    except (ValueError, TypeError):
        status, body = 400, {"error": {"code": "INVALID_REFRESH_REQUEST"}}
    except Exception:
        status, body = 503, {"error": {"code": "COMPUTE_REFRESH_UNAVAILABLE"}}
    handler._send_json_payload(
        body, status=status, headers={"Cache-Control": "no-store"},
    )
    return True


def handle_admitted_tool_inventory_refresh(
    handler: Any,
    serve_mod: Any,
    *,
    service_factory: Callable[[], Any],
) -> None:
    """Admin-only forced rediscovery (``POST /v1/tools/inventory/refresh``)."""
    if handler._reject_disallowed_origin():
        return
    if handler._auth_rate_limited():
        return
    context = handler._request_auth_context()
    if not context["authorized"]:
        handler._send_auth_error()
        return
    if not serve_mod._admin_authorized(context):
        handler._send_json_payload({"error": {"code": "FORBIDDEN"}}, status=403)
        return
    try:
        if "?" in handler.path:
            raise ValueError("refresh accepts a JSON body only")
        payload = handler._read_json(max_bytes=1024)
        status, body = dispatch_tool_inventory_refresh(service_factory, payload)
    except HTTPRequestError as error:
        status, body = error.status, {"error": {"code": "INVALID_TOOL_INVENTORY_QUERY"}}
    except (ValueError, TypeError):
        status, body = 400, {"error": {"code": "INVALID_TOOL_INVENTORY_QUERY"}}
    except Exception:
        status, body = 503, {"error": {"code": "TOOL_INVENTORY_UNAVAILABLE"}}
    handler._send_json_payload(
        body, status=status, headers={"Cache-Control": "no-store"},
    )


def _send_admin_forbidden(handler: Any, error_envelope_fn: Callable[..., Any]) -> None:
    handler._send_json_payload(
        error_envelope_fn(
            "FORBIDDEN",
            "administrator authorization is required",
            handler._correlation(),
            retryable=False,
        ),
        status=403,
    )


def dispatch_post_control_routes(
    handler: Any,
    serve_mod: Any,
    path: str,
    req: dict[str, Any],
    context: dict[str, Any],
    *,
    default_app_fn: Callable[[], Any],
    error_envelope_fn: Callable[..., Any],
    conflict_errors: tuple[type[BaseException], ...],
    not_found_errors: tuple[type[BaseException], ...],
    invalid_input_errors: tuple[type[BaseException], ...],
) -> bool:
    """Dispatch compute jobs, A2A, extensions, and durable job POST routes."""
    logger = serve_mod._serve_logger

    compute_route = serve_mod._compute_job_route(path)
    if compute_route is not None and compute_route[0] in ("submit", "cancel"):
        if not context["authorized"]:
            handler._send_auth_error()
            return True
        if not serve_mod._admin_authorized(context):
            _send_admin_forbidden(handler, error_envelope_fn)
            return True
        worker_factory = default_app_fn().compute_job_worker
        if worker_factory is None:
            logger.warning(
                "compute job submit/cancel requested but worker factory is unavailable"
            )
            handler._send_json_payload(
                {
                    "error": {
                        "message": "compute job worker is unavailable",
                        "type": "server_error",
                        "code": "COMPUTE_JOB_WORKER_UNAVAILABLE",
                    }
                },
                status=503,
            )
            return True
        operation, identity = compute_route
        try:
            if operation == "submit":
                result = dispatch_compute_job_submit(worker_factory(), req)
            else:
                reason = req.get("reason")
                if set(req) != {"reason"} or not isinstance(reason, str):
                    raise ValueError("reason must be the only request field")
                result = dispatch_compute_job_cancel(worker_factory(), identity, reason)
        except conflict_errors as error:
            handler._send_json_payload(
                {"error": {"message": str(error), "type": "conflict"}}, status=409,
            )
            return True
        except not_found_errors:
            handler._send_not_found()
            return True
        except invalid_input_errors as error:
            handler._send_json_payload(
                {"error": {"message": str(error), "type": "invalid_request"}},
                status=400,
            )
            return True
        except Exception as exc:
            logger.error(
                f"compute job request failed: operation={operation!r}, "
                f"correlation={handler._correlation()!r}",
                exc_info=True,
            )
            handler.log_error("compute job request failed: %s", type(exc).__name__)
            handler._send_json_payload(
                {
                    "error": {
                        "message": "compute job request failed",
                        "type": "server_error",
                        "code": "COMPUTE_JOB_UNAVAILABLE",
                    }
                },
                status=503,
            )
            return True
        handler._send_json_payload(result.body, status=result.status_code)
        return True

    if path == "/a2a":
        if not context["authorized"]:
            handler._send_auth_error()
            return True
        if not serve_mod._admin_authorized(context):
            _send_admin_forbidden(handler, error_envelope_fn)
            return True
        application = default_app_fn()
        a2a_handler = serve_mod._A2A_REQUEST_HANDLER or serve_mod.build_application_a2a_handler(
            application,
            base_url=serve_mod._a2a_discovery_base_url(),
            card_facade=serve_mod._A2A_AGENT_CARD_FACADE,
        )
        turn_stack = getattr(handler, "_turn_stack", None)
        if handler._operation_context is not None and turn_stack is not None:
            turn_stack.enter_context(bind_operation_context(handler._operation_context))
        a2a_result = dispatch_a2a_jsonrpc_route(a2a_handler, "POST", path, req)
        if a2a_result is None:
            handler._send_not_found()
            return True
        handler._send_json_payload(a2a_result.body, status=a2a_result.status_code)
        return True

    if serve_mod._is_extension_route(path):
        if not context["authorized"]:
            handler._send_auth_error()
            return True
        if not serve_mod._admin_authorized(context):
            _send_admin_forbidden(handler, error_envelope_fn)
            return True
        application = default_app_fn()
        facade_factory = application.extension_facade
        if facade_factory is None:
            logger.warning(
                "extension route POST requested but extension facade is unavailable"
            )
            handler._send_json_payload(
                {
                    "error": {
                        "message": "extension facade is unavailable",
                        "type": "server_error",
                        "code": "EXTENSION_FACADE_UNAVAILABLE",
                    }
                },
                status=503,
            )
            return True
        ext_result = dispatch_extension_route(
            facade_factory(), "POST", path, req, serve_mod._extension_authority(context),
        )
        if ext_result is None:
            handler._send_not_found()
            return True
        handler._send_json_payload(ext_result.body, status=ext_result.status_code)
        return True

    if path == "/v1/jobs/start":
        if not context["authorized"]:
            handler._send_auth_error()
            return True
        if not serve_mod._admin_authorized(context):
            _send_admin_forbidden(handler, error_envelope_fn)
            return True
        try:
            job_identity = JobIdentity(**serve_mod._job_start_payload(req))
            record = default_app_fn().job_service().create(job_identity)
        except ValueError as error:
            if "already exists" in str(error):
                handler._send_json_payload(
                    {
                        "error": {
                            "message": "job identity already exists",
                            "type": "conflict",
                        }
                    },
                    status=409,
                )
                return True
            handler._send_json_payload(
                {"error": {"message": str(error), "type": "invalid_request"}},
                status=400,
            )
            return True
        except TypeError as error:
            handler._send_json_payload(
                {"error": {"message": str(error), "type": "invalid_request"}},
                status=400,
            )
            return True
        except Exception as error:
            if isinstance(error, KeyError) or "already exists" in str(error):
                handler._send_json_payload(
                    {
                        "error": {
                            "message": "job identity already exists",
                            "type": "conflict",
                        }
                    },
                    status=409,
                )
                return True
            logger.error(
                f"job start failed, correlation={handler._correlation()!r}",
                exc_info=True,
            )
            handler.log_error("job start failed: %s", type(error).__name__)
            handler._send_json_payload(
                {
                    "error": {
                        "message": "job could not be started",
                        "type": "internal_error",
                    }
                },
                status=500,
            )
            return True
        handler._send_json_payload(
            {"object": "job_start", "job": serve_mod._job_record_payload(record)},
            status=202,
        )
        return True

    job_id = serve_mod._job_cancel_id(path)
    if job_id is not None:
        if not context["authorized"]:
            handler._send_auth_error()
            return True
        if not serve_mod._admin_authorized(context):
            _send_admin_forbidden(handler, error_envelope_fn)
            return True
        reason = req.get("reason")
        if (
            set(req) != {"reason"}
            or not isinstance(reason, str)
            or not reason.strip()
            or len(reason) > int(serve_mod._MAX_JOB_CANCEL_REASON)
        ):
            handler._send_json_payload(
                {
                    "error": {
                        "message": "reason must be a non-empty string of at most 256 characters",
                        "type": "invalid_request",
                    }
                },
                status=400,
            )
            return True
        service = default_app_fn().job_service()
        try:
            records = service.cancel(job_id, reason)
        except not_found_errors:
            handler._send_not_found()
            return True
        except invalid_input_errors as error:
            handler._send_json_payload(
                {"error": {"message": str(error), "type": "invalid_request"}},
                status=400,
            )
            return True
        if not records:
            handler._send_not_found()
            return True
        handler._send_json_payload({
            "object": "job_cancel",
            "job": serve_mod._job_record_payload(records[0]),
            "cancelled_count": len(records),
        })
        return True

    return False


def normalize_model_post_route(
    handler: Any,
    serve_mod: Any,
    path: str,
    req: dict[str, Any],
    context: dict[str, Any],
    *,
    record_early_chat_metric: Callable[[str], None],
    lifecycle_get: Callable[[], Any],
    error_envelope_fn: Callable[..., Any],
    invalid_input_cls: type[BaseException],
) -> tuple[bool, dict[str, Any], str, Any, Any]:
    """Validate and normalize ``/v1/chat/completions`` or ``/v1/responses`` envelope.

    Returns ``(handled_early, updated_req, model_operation, request_temperature, facade)``.
    """
    model_route = serve_mod._MODEL_REQUEST_FACADE.route(path)
    if model_route is None:
        return False, req, "", None, None
    serve_mod._serve_logger.debug(
        f"do_POST: model route matched, operation={model_route.operation!r}"
    )
    if not context["authorized"]:
        if model_route.operation == "chat.completions":
            record_early_chat_metric("unauthenticated")
        handler._send_auth_error()
        return True, req, "", None, None
    admit_chat_caller(handler, req)
    lifecycle = lifecycle_get()
    if lifecycle.coordinator.draining:
        serve_mod._serve_logger.error(
            f"request rejected: runtime is draining for shutdown, correlation={handler._correlation()!r}"
        )
        record_early_chat_metric("draining")
        handler._send_json_payload(
            error_envelope_fn(
                "DRAINING",
                "the runtime is draining for shutdown",
                handler._correlation(),
                retryable=True,
            ),
            status=503,
            headers={"Retry-After": "1"},
        )
        return True, req, "", None, None
    facade = ModelRequestFacade(
        policy_hook=lambda _operation, _payload: bool(context["authorized"]),
    )
    try:
        facade_payload = serve_mod._chat_facade_payload(req, model_route.operation)
        normalized = facade.normalize(path, facade_payload)
    except HTTPRequestError as error:
        record_early_chat_metric("invalid_messages")
        handler._send_json_payload(
            {"error": {"message": error.message, "type": error.error_type}},
            status=error.status,
        )
        return True, req, "", None, None
    except ModelFacadeError as error:
        if model_route.operation == "chat.completions":
            record_early_chat_metric("invalid_request")
        message = str(error)
        if message == "model must be a non-empty string":
            message = "model must be a string"
        elif message == "messages must be a non-empty array":
            message = "messages must be an array"
        handler._send_json_payload(
            {"error": {"message": message, "type": "invalid_request"}},
            status=error.status,
        )
        return True, req, "", None, None
    updated = dict(req)
    updated["model"] = normalized.model
    updated["messages"] = [dict(message) for message in normalized.messages]
    updated["stream"] = normalized.stream
    updated.pop("input", None)
    try:
        request_temperature = extract_request_temperature(
            normalized.options, serve_mod.server.validate_request_temperature,
        )
    except invalid_input_cls as error:
        record_early_chat_metric("invalid_temperature")
        handler._send_json_payload(
            {"error": {"message": str(error), "type": "invalid_request"}},
            status=400,
        )
        return True, updated, normalized.operation, None, facade
    return False, updated, normalized.operation, request_temperature, facade


def dispatch_post_account_and_policy_routes(
    handler: Any,
    serve_mod: Any,
    path: str,
    req: dict[str, Any],
    context: dict[str, Any],
    *,
    error_envelope_fn: Callable[..., Any],
    login_throttle_http: Any,
    has_legacy_error_prefix_fn: Callable[[Any], bool],
    thin_routes_serve: Callable[..., Any],
) -> bool:
    """Dispatch fanout, work-runs, approvals, client-tools, permission-mode, account, and thin routes."""
    if handler._handle_fanout_post(path, req, context):
        return True
    if handler._handle_work_run_request("POST", path, context=context):
        return True
    if path == "/v1/approvals" or path.startswith("/v1/approvals/"):
        handler._handle_approvals_post(path, req, context)
        return True
    if path.startswith("/v1/client-tools/"):
        handler._handle_client_tools_post(path, req, context)
        return True
    if path == "/v1/permission-mode":
        if not context["authorized"]:
            handler._send_auth_error()
            return True
        authority_error = serve_mod._system_operation_authority_error(
            "permission_mode_change", context,
        )
        if authority_error:
            handler._send_json_payload(
                error_envelope_fn(
                    "FORBIDDEN",
                    authority_error + " to change permission mode",
                    handler._correlation(),
                    retryable=False,
                ),
                status=403,
            )
            return True
        handler._handle_permission_mode_post(req, context=context)
        return True
    if path == "/v1/sonder/register":
        login_throttle_http.handle_register(
            handler,
            req,
            context,
            runtime=serve_mod.server,
            admin_auth=serve_mod.admin_auth,
            allow_registration=bool(
                serve_mod.ALLOW_REGISTRATION or serve_mod.PUBLIC_HOSTING
            ),
            created_message=serve_mod._account_created_message,
            public_hosting=bool(serve_mod.PUBLIC_HOSTING),
            extra_fields=serve_mod._auth_response_extra_fields,
        )
        return True
    if path == "/v1/sonder/login":
        login_throttle_http.handle_login(
            handler,
            req,
            context,
            runtime=serve_mod.server,
            admin_auth=serve_mod.admin_auth,
            extra_fields=serve_mod._auth_response_extra_fields,
        )
        return True
    if path == "/v1/sonder/admin/account":
        if not context["authorized"]:
            handler._send_auth_error()
            return True
        account = context["account"]
        ok, msg = serve_mod.admin_auth.require(account, "admin")
        if not ok:
            handler._send_json_payload({"ok": False, "message": msg}, status=403)
            return True
        account_header = handler.headers.get("X-Sonder-Account-Token", "")
        out = serve_mod.server.admin_set_account(
            token=serve_mod._bearer_token(
                account_header or handler.headers.get("Authorization", "")
            ),
            username=req.get("username", ""),
            role=req.get("role", ""),
            tier=req.get("tier", ""),
            dev_flags=req.get("dev_flags", ""),
            banned=str(req.get("banned", "")),
        )
        if has_legacy_error_prefix_fn(out):
            handler._send_json_payload(
                {
                    "error": {
                        "message": out.removeprefix("ERROR:").strip()
                        or "account update failed",
                        "type": "invalid_request",
                    }
                },
                status=400,
            )
            return True
        handler._send_json_payload({"ok": True, "message": out})
        return True
    thin_handler = serve_mod._THIN_HANDLERS.get(path)
    if thin_handler is not None:
        thin_routes_serve(
            handler,
            thin_handler,
            path,
            req,
            context,
            tool_refusal=serve_mod._http_tool_refusal,
        )
        return True
    return False


__all__ = [
    "dispatch_post_account_and_policy_routes",
    "dispatch_post_control_routes",
    "handle_account_logout",
    "handle_admin_drain",
    "handle_admitted_compute_refresh",
    "handle_admitted_tool_inventory_refresh",
    "normalize_model_post_route",
]
