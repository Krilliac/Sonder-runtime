"""HTTP GET/read route handlers extracted from ``serve.py``.

Covers lifecycle probes, generated-model downloads, compute snapshot/jobs,
extensions, trace projections, sessions, durable jobs, OpenAI model catalog,
admin update status, control-plane snapshots, A2A agent card discovery,
Sonder capabilities/whoami/personas/status/feed, client schema, command bar,
and compute/tool inventory reads.

Imports only ``sonder_runtime.application``, ``sonder_runtime.interfaces``,
and non-IO stdlib modules.
"""
from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sonder_runtime.application.execution.world_control import OutputWatermark
from .client_protocol import (
    SCHEMA_ROUTE as CLIENT_SCHEMA_ROUTE,
    schema_response as client_schema_response,
)
from .compute_fabric import (
    dispatch_compute_job_artifact,
    dispatch_compute_job_by_idempotency,
    dispatch_compute_job_status,
    dispatch_compute_snapshot,
)
from .compute_inventory import dispatch_compute_inventory
from .config_wiring import HTTPRequestError
from .extensions import dispatch_extension_route
from .host_tools import dispatch_tool_inventory
from .observability import dispatch_trace_route
from .session import dispatch_session_route


def handle_lifecycle_get(
    handler: Any,
    path: str,
    *,
    lifecycle_get: Callable[[], Any],
    health_facade: Any,
    logger: Any,
) -> bool:
    """SPEC-2 WP3 lifecycle/health/metrics GET routes."""
    lifecycle = lifecycle_get()
    route = health_facade.route(path)
    if route is None:
        return False
    logger.debug(
        f"_handle_lifecycle_get: path={path!r}, requires_auth={route.requires_auth}"
    )
    if route.requires_auth and not handler._peer_is_loopback():
        if handler._auth_rate_limited():
            return True
        if not handler._request_auth_context()["authorized"]:
            logger.debug(f"_handle_lifecycle_get: auth failed for path={path!r}")
            handler._send_auth_error()
            return True
    status, payload = route.render(lifecycle)
    if route.media_type == "application/json":
        handler._send_json_payload(payload, status=status)
        return True
    body = payload
    must_close = bool(handler._close_for_unread_body())
    handler.send_response(200)
    handler._cors()
    if must_close:
        handler.send_header("Connection", "close")
    handler.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)
    return True


def handle_ollama_pool_admin(
    handler: Any,
    serve_mod: Any,
    method: str,
    *,
    parse_query: Callable[[str, int], dict[str, list[str]]],
) -> None:
    """Admin-gated ``GET`` / ``POST /v1/sonder/ollama-pool`` handler."""
    if handler._auth_rate_limited():
        return
    context = handler._request_auth_context()
    if not context.get("authorized"):
        handler._send_auth_error()
        return
    if not serve_mod._admin_authorized(context):
        handler._send_json_payload(
            {"error": "authorization"},
            status=403,
            headers={"Cache-Control": "no-store"},
        )
        return
    try:
        if method == "GET":
            values = parse_query(handler.path, 2)
            if set(values) - {"cursor", "page_size"} or any(
                len(v) != 1 for v in values.values()
            ):
                raise ValueError("invalid page query")
            params: dict[str, Any] = {
                key: value[0] for key, value in values.items()
            }
            if "page_size" in params:
                params["page_size"] = int(params["page_size"])
        else:
            params = handler._read_json()
        result, status = serve_mod._ollama_pool_admin_page(context, params)
    except (ValueError, HTTPRequestError):
        result, status = {"error": "invalid_request"}, 400
    handler._send_json_payload(
        result, status=status, headers={"Cache-Control": "no-store"},
    )


def handle_generated_model_get(
    handler: Any,
    path: str,
    *,
    model3d_generation: Any,
    parse_query: Callable[[str], dict[str, list[str]]],
) -> bool:
    """Serve signed or authenticated ``GET /v1/generated-models/<file>`` downloads."""
    if not path.startswith("/v1/generated-models/"):
        return False
    filename = path[len("/v1/generated-models/") :]
    query_params = parse_query(handler.path)
    dl_ticket = (query_params.get("dl") or [""])[0]
    ticket_ok = model3d_generation.verify_model_download_ticket(filename, dl_ticket)
    if not ticket_ok:
        context = handler._request_auth_context()
        if not context["authorized"]:
            handler._send_auth_error()
            return True
    loaded = model3d_generation.read_generated_model_bytes(filename)
    if loaded is None:
        handler._send_not_found()
        return True
    safe_name, raw_bytes = loaded
    ext = Path(safe_name).suffix.lower()
    ctype = {
        ".glb": "model/gltf-binary",
        ".obj": "text/plain; charset=utf-8",
        ".png": "image/png",
    }.get(ext, "application/octet-stream")
    handler._send_binary_payload(
        raw_bytes,
        content_type=ctype,
        digest=hashlib.sha256(raw_bytes).hexdigest(),
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}"',
            "X-Content-Type-Options": "nosniff",
        },
    )
    return True


def handle_commands_get(
    handler: Any,
    serve_mod: Any,
    *,
    split_url: Callable[[str], tuple[str, str]],
    parse_qs: Callable[[str], dict[str, list[str]]],
) -> bool:
    """Read-only ``GET /v1/commands[/complete|/help]`` endpoints."""
    raw_path, raw_query = split_url(handler.path)
    route = raw_path.rstrip("/") or "/"
    if route not in ("/v1/commands", "/v1/commands/complete", "/v1/commands/help"):
        return False
    context = handler._request_auth_context()
    if not context["authorized"]:
        handler._send_auth_error()
        return True
    query = parse_qs(raw_query)

    def first(name: str) -> str:
        values = query.get(name) or [""]
        return values[0]

    if route == "/v1/commands/complete":
        payload = serve_mod._commands_complete_payload(
            first("q"), first("limit"), context=context,
        )
    elif route == "/v1/commands/help":
        payload = serve_mod._commands_help_payload(first("topic"), context=context)
    else:
        payload = serve_mod._commands_index_payload(context=context)
    handler._send_json_payload(payload)
    return True


def handle_compute_inventory_read(
    handler: Any,
    serve_mod: Any,
    *,
    default_app_fn: Callable[[], Any],
    parse_query: Callable[[str, int], dict[str, list[str]]],
) -> None:
    """Admin-only ``GET /v1/compute/nodes`` handler."""
    context = handler._request_auth_context()
    if not context["authorized"]:
        handler._send_auth_error()
        return
    if not serve_mod._admin_authorized(context):
        handler._send_json_payload({"error": {"code": "FORBIDDEN"}}, status=403)
        return
    try:
        handler._validate_request_framing()
        if handler._unread_request_body_bytes() != 0:
            raise ValueError("inventory reads do not accept a body")
        query = parse_query(handler.path, 2)
        dispatch_fn: Callable[..., tuple[int, Any]] = dispatch_compute_inventory
        status, body = dispatch_fn(default_app_fn().compute_inventory_page, query)
    except (ValueError, HTTPRequestError):
        status, body = 400, {"error": {"code": "INVALID_INVENTORY_QUERY"}}
    except Exception:
        status, body = 503, {"error": {"code": "COMPUTE_INVENTORY_UNAVAILABLE"}}
    handler._send_json_payload(
        body, status=status, headers={"Cache-Control": "no-store"},
    )


def handle_tool_inventory_read(
    handler: Any,
    serve_mod: Any,
    *,
    service_factory: Callable[[], Any],
    parse_query: Callable[[str, int], dict[str, list[str]]],
) -> None:
    """Admin-only redacted host tool inventory (``GET /v1/tools/inventory``)."""
    context = handler._request_auth_context()
    if not context["authorized"]:
        handler._send_auth_error()
        return
    if not serve_mod._admin_authorized(context):
        handler._send_json_payload({"error": {"code": "FORBIDDEN"}}, status=403)
        return
    try:
        handler._validate_request_framing()
        if handler._unread_request_body_bytes() != 0:
            raise ValueError("tool inventory reads do not accept a body")
        query = parse_query(handler.path, 2)
        status, body = dispatch_tool_inventory(service_factory, query)
    except (ValueError, HTTPRequestError):
        status, body = 400, {"error": {"code": "INVALID_TOOL_INVENTORY_QUERY"}}
    except Exception:
        status, body = 503, {"error": {"code": "TOOL_INVENTORY_UNAVAILABLE"}}
    handler._send_json_payload(
        body, status=status, headers={"Cache-Control": "no-store"},
    )


def _send_admin_forbidden(
    handler: Any, *, envelope_fn: Callable[..., Any] | None = None,
) -> None:
    if envelope_fn is not None:
        handler._send_json_payload(
            envelope_fn(
                "FORBIDDEN",
                "administrator authorization is required",
                handler._correlation(),
                retryable=False,
            ),
            status=403,
        )
    else:
        handler._send_json_payload(
            {
                "error": {
                    "message": "administrator authorization is required",
                    "type": "forbidden",
                    "code": "FORBIDDEN",
                }
            },
            status=403,
        )


def dispatch_authenticated_get(
    handler: Any,
    serve_mod: Any,
    path: str,
    *,
    default_app_fn: Callable[[], Any],
    update_status_fn: Callable[[], Any],
    parse_query: Callable[[str], dict[str, list[str]]],
    raw_query_of: Callable[[str], str],
    error_envelope_fn: Callable[..., Any],
    lifecycle_get: Callable[[], Any],
    personas_mod: Any,
    default_home_fn: Callable[[], Any],
    build_operational_capabilities_fn: Callable[..., Any],
    narrated_status_fn: Callable[..., Any],
    not_found_errors: tuple[type[BaseException], ...],
    invalid_input_errors: tuple[type[BaseException], ...],
) -> None:
    """Dispatch GET routes after developer/inventory/debug routes."""
    logger = serve_mod._serve_logger

    if path == "/v1/compute/snapshot":
        context = handler._request_auth_context()
        if not context["authorized"]:
            handler._send_auth_error()
            return
        if not serve_mod._admin_authorized(context):
            _send_admin_forbidden(handler)
            return
        snapshot_factory = default_app_fn().compute_snapshot
        if snapshot_factory is None:
            logger.warning("compute snapshot requested but factory is unavailable")
            handler._send_json_payload(
                {
                    "error": {
                        "message": "compute snapshot is unavailable",
                        "type": "server_error",
                        "code": "COMPUTE_SNAPSHOT_UNAVAILABLE",
                    }
                },
                status=503,
            )
            return
        try:
            snapshot_result = dispatch_compute_snapshot(snapshot_factory)
        except Exception as exc:
            logger.error(
                f"compute snapshot failed, correlation={handler._correlation()!r}",
                exc_info=True,
            )
            handler.log_error("compute snapshot failed: %s", type(exc).__name__)
            handler._send_json_payload(
                {
                    "error": {
                        "message": "compute snapshot is unavailable",
                        "type": "server_error",
                        "code": "COMPUTE_SNAPSHOT_UNAVAILABLE",
                    }
                },
                status=503,
            )
            return
        handler._send_json_payload(
            snapshot_result.body, status=snapshot_result.status_code,
        )
        return

    compute_route = serve_mod._compute_job_route(path)
    if compute_route is not None and compute_route[0] in (
        "status",
        "by_idempotency",
        "artifact",
    ):
        context = handler._request_auth_context()
        if not context["authorized"]:
            handler._send_auth_error()
            return
        if not serve_mod._admin_authorized(context):
            _send_admin_forbidden(handler)
            return
        worker_factory = default_app_fn().compute_job_worker
        if worker_factory is None:
            logger.warning("compute job worker requested but factory is unavailable")
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
            return
        operation, identity = compute_route
        job_result: Any = None
        try:
            if operation == "status":
                job_result = dispatch_compute_job_status(worker_factory(), identity)
            elif operation == "by_idempotency":
                job_result = dispatch_compute_job_by_idempotency(
                    worker_factory(), identity,
                )
            else:
                remote_job_id, artifact_name = identity
                artifact_result = dispatch_compute_job_artifact(
                    worker_factory(), remote_job_id, artifact_name,
                )
                artifact = artifact_result.payload
                handler._send_binary_payload(
                    artifact.content,
                    content_type=artifact.receipt.mime_type,
                    digest=artifact.receipt.sha256,
                    status=artifact_result.status_code,
                )
                return
        except not_found_errors:
            handler._send_not_found()
            return
        except invalid_input_errors as error:
            handler._send_json_payload(
                {"error": {"message": str(error), "type": "invalid_request"}},
                status=400,
            )
            return
        except Exception as exc:
            logger.error(
                f"compute job status failed: operation={operation!r}, "
                f"correlation={handler._correlation()!r}",
                exc_info=True,
            )
            handler.log_error("compute job status failed: %s", type(exc).__name__)
            handler._send_json_payload(
                {
                    "error": {
                        "message": "compute job status is unavailable",
                        "type": "server_error",
                        "code": "COMPUTE_JOB_UNAVAILABLE",
                    }
                },
                status=503,
            )
            return
        if job_result is None:
            handler._send_not_found()
            return
        handler._send_json_payload(job_result.body, status=job_result.status_code)
        return

    if serve_mod._is_extension_route(path):
        context = handler._request_auth_context()
        if not context["authorized"]:
            handler._send_auth_error()
            return
        if not serve_mod._admin_authorized(context):
            _send_admin_forbidden(handler)
            return
        application = default_app_fn()
        facade_factory = application.extension_facade
        if facade_factory is None:
            logger.warning(
                "extension route GET requested but extension facade is unavailable"
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
            return
        ext_result = dispatch_extension_route(
            facade_factory(), "GET", path, None, serve_mod._extension_authority(context),
        )
        if ext_result is None:
            handler._send_not_found()
            return
        handler._send_json_payload(ext_result.body, status=ext_result.status_code)
        return

    if serve_mod._is_trace_projection_route(path):
        context = handler._request_auth_context()
        if not context["authorized"]:
            handler._send_auth_error()
            return
        if not serve_mod._admin_authorized(context):
            _send_admin_forbidden(handler)
            return
        query = parse_query(handler.path)
        trace_result = dispatch_trace_route(default_app_fn().events, "GET", path, query)
        if trace_result is None:
            handler._send_not_found()
            return
        handler._send_json_payload(trace_result.body, status=trace_result.status_code)
        return

    if path in serve_mod._TELEMETRY_ROUTES:
        handler._handle_telemetry_get(path)
        return

    serve_mod._maybe_live_reload()

    if path == "/v1/sessions" or path.startswith("/v1/sessions/"):
        context = handler._request_auth_context()
        if not context["authorized"]:
            handler._send_auth_error()
            return
        if not serve_mod._admin_authorized(context):
            _send_admin_forbidden(handler)
            return
        facade = serve_mod._SESSION_FACADE
        if facade is None:
            logger.warning("session route requested but session facade is not configured")
            handler._send_json_payload(
                {
                    "error": {
                        "message": "session facade is unavailable",
                        "type": "server_error",
                        "code": "SESSION_FACADE_UNAVAILABLE",
                    }
                },
                status=503,
            )
            return
        query_params = parse_query(handler.path)
        session_result = dispatch_session_route(facade, path, query=query_params)
        if session_result is None:
            handler._send_not_found()
            return
        if (
            session_result.status_code == 200
            and path.startswith("/v1/sessions/")
            and isinstance(session_result.body, dict)
            and session_result.body.get("recovered_sequence") == 0
        ):
            parts = path.split("?", 1)[0].rstrip("/")[len("/v1/sessions/") :].split("/")
            if len(parts) == 2 and parts[1] == "replay":
                scoped_id = serve_mod._hosted_storage_id(context, parts[0], "session")
                if scoped_id and scoped_id != parts[0]:
                    alt = dispatch_session_route(
                        facade, f"/v1/sessions/{scoped_id}/replay", query=query_params,
                    )
                    if (
                        alt is not None
                        and alt.status_code == 200
                        and isinstance(alt.body, dict)
                        and int(alt.body.get("recovered_sequence") or 0) > 0
                    ):
                        session_result = alt
        handler._send_json_payload(
            session_result.body, status=session_result.status_code,
        )
        return

    if path == "/v1/jobs" or path.startswith("/v1/jobs/"):
        context = handler._request_auth_context()
        if not context["authorized"]:
            handler._send_auth_error()
            return
        if not serve_mod._admin_authorized(context):
            _send_admin_forbidden(handler, envelope_fn=error_envelope_fn)
            return
        application = default_app_fn()
        stream_job_id = serve_mod._job_subroute_id(path, "/stream")
        result_job_id = serve_mod._job_subroute_id(path, "/result")
        if stream_job_id is not None:
            try:
                after, max_events, max_bytes = serve_mod._job_stream_query(
                    raw_query_of(handler.path)
                )
            except ValueError as error:
                handler._send_json_payload(
                    {"error": {"message": str(error), "type": "invalid_request"}},
                    status=400,
                )
                return
            try:
                page = application.job_registry().stream(
                    stream_job_id,
                    after=OutputWatermark(after),
                    max_events=max_events,
                    max_bytes=max_bytes,
                )
            except KeyError:
                handler._send_not_found()
                return
            handler._send_json_payload({
                "object": "job_output",
                "job_id": stream_job_id,
                "events": [
                    serve_mod._job_output_event_payload(event) for event in page.events
                ],
                "next_watermark": page.next_watermark.sequence,
                "has_more": page.has_more,
                "truncated": page.truncated,
            })
            return
        if result_job_id is not None:
            try:
                record = application.job_registry().collect(result_job_id)
            except KeyError:
                handler._send_not_found()
                return
            except ValueError as error:
                handler._send_json_payload(
                    {"error": {"message": str(error), "type": "conflict"}},
                    status=409,
                )
                return
            handler._send_json_payload({
                "object": "job_result",
                "job_id": result_job_id,
                "status": record.status.value,
                "result": record.result,
                "error": record.error,
                "job": serve_mod._job_record_payload(record),
            })
            return

        service = application.job_service()
        if path == "/v1/jobs":
            query = parse_query(handler.path)
            raw_limit = query.get("limit", ["100"])[-1]
            try:
                limit = int(raw_limit)
            except (TypeError, ValueError):
                handler._send_json_payload(
                    {
                        "error": {
                            "message": "limit must be an integer between 1 and 100",
                            "type": "invalid_request",
                        }
                    },
                    status=400,
                )
                return
            if limit < 1 or limit > 100:
                handler._send_json_payload(
                    {
                        "error": {
                            "message": "limit must be an integer between 1 and 100",
                            "type": "invalid_request",
                        }
                    },
                    status=400,
                )
                return
            records = service.list(limit=limit)
            handler._send_json_payload({
                "object": "list",
                "data": [serve_mod._job_record_payload(record) for record in records],
            })
            return
        job_id = serve_mod._job_record_id(path)
        if job_id is None:
            handler._send_not_found()
            return
        try:
            record = service.get(job_id)
        except not_found_errors:
            handler._send_not_found()
            return
        handler._send_json_payload(serve_mod._job_record_payload(record))
        return

    if path == "/v1/models":
        context = handler._request_auth_context()
        if not context["authorized"]:
            handler._send_auth_error()
            return
        handler._send_json_payload(
            {"object": "list", "data": serve_mod._openai_model_rows()}
        )
        return

    if path == "/v1/admin/updates/status":
        context = handler._request_auth_context()
        if not context["authorized"]:
            handler._send_auth_error()
            return
        if not serve_mod._admin_authorized(context):
            _send_admin_forbidden(handler, envelope_fn=error_envelope_fn)
            return
        try:
            payload = update_status_fn()
        except Exception as error:
            logger.error(
                f"update status failed, correlation={handler._correlation()!r}",
                exc_info=True,
            )
            handler.log_error("update status failed: %s", type(error).__name__)
            handler._send_json_payload(
                error_envelope_fn(
                    "INTERNAL",
                    "update status is unavailable",
                    handler._correlation(),
                    retryable=True,
                ),
                status=500,
            )
            return
        handler._send_json_payload(payload)
        return

    if serve_mod._CONTROL_PLANE_FACADE.route(path) is not None:
        context = handler._request_auth_context()
        if not context["authorized"]:
            handler._send_auth_error()
            return
        if not serve_mod._admin_authorized(context):
            _send_admin_forbidden(handler, envelope_fn=error_envelope_fn)
            return
        route = serve_mod._CONTROL_PLANE_FACADE.route(path)
        if serve_mod._CONTROL_PLANE_SERVICE is None:
            handler._send_json_payload({"error": "control_plane_unavailable"}, status=503)
            return
        status, payload = route.render(
            serve_mod._CONTROL_PLANE_SERVICE,
            captured_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        handler._send_json_payload(payload, status=status)
        return

    if serve_mod._A2A_AGENT_CARD_FACADE.route(path) is not None:
        context = handler._request_auth_context()
        if not context["authorized"]:
            handler._send_auth_error()
            return
        if not serve_mod._admin_authorized(context):
            _send_admin_forbidden(handler, envelope_fn=error_envelope_fn)
            return
        base_url = serve_mod._a2a_discovery_base_url()
        if not base_url:
            handler._send_json_payload({"error": "a2a_discovery_unavailable"}, status=503)
            return
        application = default_app_fn()
        registry_factory = application.agent_registry
        if registry_factory is None:
            handler._send_json_payload({"error": "a2a_discovery_unavailable"}, status=503)
            return
        card = serve_mod._A2A_AGENT_CARD_FACADE.card(
            registry_factory().registrations,
            base_url=base_url,
        )
        route = serve_mod._A2A_AGENT_CARD_FACADE.route(path)
        status, payload = route.render(card)
        handler._send_json_payload(payload, status=status)
        return

    if path == "/v1/sonder/capabilities":
        context = handler._request_auth_context()
        if (
            not context.get("authorized")
            and context.get("mode") != "local-open"
            and (
                str(handler.headers.get("Authorization", "") or "").strip()
                or str(handler.headers.get("X-Sonder-Account-Token", "") or "").strip()
            )
        ):
            handler._record_auth_failure("invalid-credentials")
        handler._send_json_payload(serve_mod._capabilities_payload(context))
        return

    if path == "/v1/sonder/whoami":
        context = handler._request_auth_context()
        if not context["authorized"]:
            handler._send_auth_error()
            return
        handler._send_json_payload(
            serve_mod.server._http_chat_tools().whoami_payload(
                context,
                auth_mode=serve_mod._effective_auth_mode(),
                public_hosting=bool(serve_mod.PUBLIC_HOSTING),
                admin_authorized=serve_mod._admin_authorized(context),
            )
        )
        return

    if path == "/v1/sonder/personas":
        context = handler._request_auth_context()
        if not context["authorized"]:
            handler._send_auth_error()
            return
        descs = personas_mod.descriptions()
        handler._send_json_payload({
            "default": personas_mod.AUTO,
            "modes": list(personas_mod.MODES),
            "personas": [
                {"name": name, "description": descs.get(name, "")}
                for name in personas_mod.names()
            ],
        })
        return

    if path == "/v1/sonder/status":
        context = handler._request_auth_context()
        if not context["authorized"]:
            handler._send_auth_error()
            return
        status_query = parse_query(handler.path)
        status_session = str(
            (status_query.get("session") or [""])[0]
            or handler.headers.get("X-Sonder-Session", "")
            or ""
        ).strip()
        status_project = str((status_query.get("project") or [""])[0] or "").strip()
        handler._send_json_payload(
            serve_mod.server._http_chat_tools().sonder_status_payload(
                server=serve_mod.server,
                context=context,
                caps=serve_mod._capabilities_payload(context),
                public_hosting=bool(serve_mod.PUBLIC_HOSTING),
                admin_authorized=serve_mod._admin_authorized(context),
                detail_allowed=serve_mod._execution_feed_detail_allowed(context),
                memory_receiver_configured=(
                    serve_mod._MEMORY_REPLICATION_RECEIVER is not None
                ),
                app_control_binding=serve_mod._APP_CONTROL_BINDING,
                state_home=str(default_home_fn()),
                deployment_payload=lifecycle_get().deployment_payload(),
                build_operational_capabilities=build_operational_capabilities_fn,
                narrate=narrated_status_fn,
                session=status_session,
                project=status_project,
            )
        )
        return

    if path == CLIENT_SCHEMA_ROUTE:
        context = handler._request_auth_context()
        if not context["authorized"]:
            handler._send_auth_error()
            return
        status, body = client_schema_response(serve_mod._client_protocol_host())
        handler._send_json_payload(body, status=status)
        return

    if path == "/v1/sonder/feed":
        context = handler._request_auth_context()
        if not context["authorized"]:
            handler._send_auth_error()
            return
        handler._send_json_payload(
            serve_mod.server.activity_tracker.live_feed_for_owner(
                serve_mod._feed_request_owner(context)
            )
        )
        return

    if handler._handle_commands_get():
        return
    if handler._handle_approvals_get():
        return
    if handler._handle_permission_mode_get():
        return
    if handler._handle_fanout_get():
        return
    if handler._handle_work_run_request("GET", path):
        return
    handler._send_not_found()


__all__ = [
    "dispatch_authenticated_get",
    "handle_commands_get",
    "handle_compute_inventory_read",
    "handle_generated_model_get",
    "handle_lifecycle_get",
    "handle_ollama_pool_admin",
    "handle_tool_inventory_read",
]
