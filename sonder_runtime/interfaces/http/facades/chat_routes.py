"""HTTP chat-completion (and normalized responses) execution and SSE streaming.

Extracted from ``sonder_runtime/interfaces/http/serve.py``.  Imports only
``sonder_runtime.application``, ``sonder_runtime.interfaces``, and non-IO
standard-library modules.  Generation call-site callbacks (natural fanout,
natural ensemble, admission slot) are supplied from ``serve.py`` so every
call-site registry and fairness invariant remains exact.
"""
from __future__ import annotations

import contextlib
import json
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from sonder_runtime.application.chat.handoff_receipts import ChatWorkResult
from sonder_runtime.application.chat.provider_bridge import degradation_scope
from sonder_runtime.application.routing.long_context_overflow import (
    notice_scope as overflow_notice_scope,
    receipt_entry as overflow_receipt_entry,
)
from ..chat_tool_stream import (
    request_mode_scope as chat_request_mode_scope,
    slash_messages as chat_slash_messages,
    write_tool_chunk as chat_write_tool_chunk,
)
from ..client_tools import merge_client_tool_receipt
from ..media_routes import run_chat_media_route
from ..serve_policy import (
    maybe_temperature_override,
    serve_temperature,
    serve_temperature_override,
)
from ..sse import KEEPALIVE_FRAME, SSEKeepAlive
from ...orchestration_commands import reply_receipt_fields
from .approvals import turn_refusal_receipt
from .config_wiring import HTTPRequestError, LiveSessionCaptureFailure
from .saas_auth import rate_limit_and_meter


@dataclass
class ChatTurnDeps:
    """Explicit adapter/platform/domain collaborators injected from ``serve.py``."""

    lifecycle_mod: Any
    context_policy_parse_strict: Callable[[Any], Any]
    chat_temperature_mod: Any
    chat_effort_mod: Any
    personas_mod: Any
    image_generation_mod: Any
    model3d_generation_mod: Any
    thinking_controls_mod: Any
    reference_lookup_lanes_mod: Any
    chat_formatting_mod: Any
    load_spanda_http: Callable[[], Any]
    acquire_chat_slot: Callable[[Any], Any]
    run_natural_fanout: Callable[[dict[str, Any]], Any]
    run_natural_ensemble: Callable[[dict[str, Any], str], Any]


def handle_chat_completions(
    handler: Any,
    serve_mod: Any,
    req: dict[str, Any],
    context: dict[str, Any],
    *,
    model_operation: str,
    request_temperature: Any,
    facade: Any,
    record_early_chat_metric: Callable[[str], None],
    deps: ChatTurnDeps,
) -> None:
    """Validate, admit, execute, and respond to a chat completion request."""
    logger = serve_mod._serve_logger
    runtime = serve_mod.server
    logger.debug("do_POST: handling chat completions")

    if not context["authorized"]:
        record_early_chat_metric("unauthenticated")
        handler._send_auth_error()
        return

    account = context["account"]
    try:
        messages = serve_mod._validate_chat_messages(req.get("messages"))
    except HTTPRequestError as error:
        record_early_chat_metric("invalid_messages")
        handler._send_json_payload(
            {"error": {"message": error.message, "type": error.error_type}},
            status=error.status,
        )
        return

    structured_schema = None
    if "response_format" in req:
        try:
            structured_schema = serve_mod._response_format_schema(req["response_format"])
        except HTTPRequestError as error:
            record_early_chat_metric("invalid_response_format")
            handler._send_json_payload(
                {"error": {"message": error.message, "type": error.error_type}},
                status=error.status,
            )
            return

    model: Any = req.get("model", "sonder")
    if isinstance(model, str):
        model = model.strip()
    if not isinstance(model, str):
        record_early_chat_metric("invalid_model")
        handler._send_json_payload(
            {"error": {"message": "model must be a string", "type": "invalid_request"}},
            status=400,
        )
        return

    prompt = serve_mod._last_user_message(messages)
    natural_model = (
        runtime.natural_model_request(prompt)
        if model_operation != "responses" and serve_mod._uses_default_model_route(model)
        else None
    )
    if structured_schema is not None and natural_model is not None:
        record_early_chat_metric("structured_control_route")
        handler._send_json_payload(
            {
                "error": {
                    "message": "response_format is unavailable for natural model/control routes",
                    "type": "invalid_request",
                }
            },
            status=400,
        )
        return
    if natural_model:
        selected_prompt = natural_model["prompt"]
        if selected_prompt.lstrip().startswith("/"):
            record_early_chat_metric("wrapped_slash_command")
            handler._send_json_payload(
                {
                    "error": {
                        "message": "model selection cannot wrap a slash command; issue the command directly.",
                        "type": "invalid_request",
                    }
                },
                status=400,
            )
            return
        if natural_model["kind"] == "model":
            prompt = selected_prompt
    if structured_schema is not None and prompt.lstrip().startswith("/"):
        record_early_chat_metric("structured_control_route")
        handler._send_json_payload(
            {
                "error": {
                    "message": "response_format is unavailable for slash/tool/control routes",
                    "type": "invalid_request",
                }
            },
            status=400,
        )
        return

    client_exec, client_exec_err = runtime._http_chat_tools().negotiate_client_exec(
        req,
        handler.headers,
        public_hosting=bool(serve_mod.PUBLIC_HOSTING),
        restricted_role=serve_mod._restricted_tool_role(context),
    )
    if client_exec_err:
        record_early_chat_metric("invalid_request")
        handler._send_json_payload(
            {"error": {"message": client_exec_err, "type": "invalid_request"}},
            status=400,
        )
        return

    client_slash_plan = runtime._http_chat_tools().resolve_client_slash_for_request(
        prompt, client_exec=client_exec, model_operation=model_operation,
    )
    if (
        serve_mod._dangerous_http_slash(prompt)
        and client_slash_plan is None
        and not serve_mod._developer_authorized(context)
    ):
        record_early_chat_metric("forbidden_command")
        handler._send_json_payload(
            {
                "error": {
                    "message": "developer or admin authentication is required",
                    "type": "forbidden_command",
                }
            },
            status=403,
        )
        return

    conn = runtime._open_db()
    try:
        ok, msg = rate_limit_and_meter(conn, account, serve_mod.admin_auth)
    finally:
        conn.close()
    if not ok:
        record_early_chat_metric("rate_limited")
        handler._send_json_payload(
            {"error": {"message": msg, "type": "rate_limit"}}, status=429,
        )
        return

    stream: Any = req.get("stream", False)
    if stream is None:
        stream = False
    elif not isinstance(stream, bool):
        record_early_chat_metric("invalid_stream")
        handler._send_json_payload(
            {"error": {"message": "stream must be a boolean", "type": "invalid_request"}},
            status=400,
        )
        return

    include_stream_usage = False
    if "stream_options" in req:
        stream_options = req["stream_options"]
        if not isinstance(stream_options, dict):
            record_early_chat_metric("invalid_stream_options")
            handler._send_json_payload(
                {
                    "error": {
                        "message": "stream_options must be an object",
                        "type": "invalid_request",
                    }
                },
                status=400,
            )
            return
        include_usage = stream_options.get("include_usage", False)
        if not isinstance(include_usage, bool):
            record_early_chat_metric("invalid_stream_options")
            handler._send_json_payload(
                {
                    "error": {
                        "message": "stream_options.include_usage must be a boolean",
                        "type": "invalid_request",
                    }
                },
                status=400,
            )
            return
        include_stream_usage = include_usage

    if natural_model and natural_model["kind"] in ("fanout", "ensemble"):
        if not serve_mod._developer_authorized(context):
            record_early_chat_metric(
                "ensemble_forbidden"
                if natural_model["kind"] == "ensemble"
                else "fanout_forbidden"
            )
            handler._send_json_payload(
                {
                    "error": {
                        "message": "developer or admin authentication is required for model ensembles",
                        "type": "forbidden_command",
                    }
                },
                status=403,
            )
            return
    if natural_model and natural_model["kind"] == "model":
        model = natural_model["model"]

    model_selector = serve_mod._request_model_selector(model)
    logger.debug(
        f"do_POST: model={model!r}, selector={model_selector!r}, stream={req.get('stream', False)}"
    )
    model_error = serve_mod._chat_model_selection_error(model_selector)
    if model_error:
        status, message = model_error
        record_early_chat_metric("invalid_model")
        handler._send_json_payload(
            {
                "error": {
                    "message": message,
                    "type": "forbidden" if status == 403 else "invalid_request",
                }
            },
            status=status,
        )
        return

    try:
        runtime.prewarm_model(model_selector or "")
    except Exception:
        logger.error(
            f"model prewarm failed for selector={model_selector!r}", exc_info=True,
        )

    context_size = req.get("context_size", "")
    if context_size is None:
        context_size = ""
    if context_size != "" and (
        isinstance(context_size, bool)
        or not isinstance(context_size, (str, int))
        or deps.context_policy_parse_strict(context_size) is None
    ):
        record_early_chat_metric("invalid_context_size")
        handler._send_json_payload(
            {
                "error": {
                    "message": (
                        "context_size must be a positive token count, optionally "
                        "suffixed k or m (e.g. 8192, 32k, 1m)"
                    ),
                    "type": "invalid_request",
                }
            },
            status=400,
        )
        return

    location_consent = req.get("location_consent") is True
    location_hint = req.get("location_hint")
    if location_hint is not None and not isinstance(location_hint, dict):
        record_early_chat_metric("invalid_location_hint")
        handler._send_json_payload(
            {
                "error": {
                    "message": "location_hint must be an object",
                    "type": "invalid_request",
                }
            },
            status=400,
        )
        return

    try:
        session = serve_mod._http_scope_value(req.get("session", ""), "session")
        project = serve_mod._http_scope_value(req.get("project", ""), "project")
        storage_session = serve_mod._hosted_storage_id(context, session, "session")
        storage_project = serve_mod._hosted_storage_id(context, project, "project")
    except HTTPRequestError as error:
        record_early_chat_metric("invalid_scope")
        handler._send_json_payload(
            {"error": {"message": error.message, "type": error.error_type}},
            status=error.status,
        )
        return

    invalid_input_cls = serve_mod.InvalidInput
    try:
        apply_session_temperature, session_temperature = (
            deps.chat_temperature_mod.session_field(
                req.get("session_temperature"), storage_session,
            )
        )
    except invalid_input_cls as error:
        record_early_chat_metric("invalid_session_temperature")
        handler._send_json_payload(
            {
                "error": {
                    "message": "session_temperature: %s" % error,
                    "type": "invalid_request",
                }
            },
            status=400,
        )
        return
    try:
        request_reasoning_effort = (
            deps.chat_effort_mod.check_effort(req.get("reasoning_effort"))
            if req.get("reasoning_effort") is not None
            else None
        )
    except invalid_input_cls as error:
        record_early_chat_metric("invalid_reasoning_effort")
        handler._send_json_payload(
            {
                "error": {
                    "message": "reasoning_effort: %s" % error,
                    "type": "invalid_request",
                }
            },
            status=400,
        )
        return
    try:
        apply_session_effort, session_reasoning_effort = (
            deps.chat_effort_mod.session_field(
                req.get("session_reasoning_effort"), storage_session,
            )
        )
    except invalid_input_cls as error:
        record_early_chat_metric("invalid_session_reasoning_effort")
        handler._send_json_payload(
            {
                "error": {
                    "message": "session_reasoning_effort: %s" % error,
                    "type": "invalid_request",
                }
            },
            status=400,
        )
        return

    request_persona = None
    if req.get("persona") is not None:
        request_persona = deps.personas_mod.normalize_selection(req.get("persona"))
        if request_persona is None:
            record_early_chat_metric("invalid_persona")
            handler._send_json_payload(
                {
                    "error": {
                        "message": "persona: expected one of %s"
                        % ", ".join(
                            list(deps.personas_mod.MODES) + deps.personas_mod.names()
                        ),
                        "type": "invalid_request",
                    }
                },
                status=400,
            )
            return

    history_source = req.get("history")
    if history_source is not None and history_source not in serve_mod._CHAT_HISTORY_SOURCES:
        record_early_chat_metric("invalid_history")
        handler._send_json_payload(
            {
                "error": {
                    "message": (
                        'history must be "client" (use only the messages '
                        'sent) or "auto" (the default)'
                    ),
                    "type": "invalid_request",
                }
            },
            status=400,
        )
        return

    history = serve_mod._history_from_messages(messages)
    if not history and storage_session and history_source != "client":
        history = serve_mod._server_side_history(storage_session)
    if history:
        history = [
            {
                **item,
                "content": deps.image_generation_mod.strip_inline_data_images(
                    item.get("content", "")
                ),
            }
            if isinstance(item, dict) and isinstance(item.get("content"), str)
            else item
            for item in history
        ]

    account_header = handler.headers.get("X-Sonder-Account-Token", "")
    auth_header = handler.headers.get("Authorization", "")
    state, state_pinned = serve_mod._acquire_http_conversation_state(
        context,
        session,
        token=serve_mod._request_account_token(context, auth_header, account_header),
        pin=True,
    )

    reply: Any = None
    content: Any = ""
    web_routed = False
    execution_routed = False
    turn: Any = None
    response_iid: str | None = None
    response_reasoning = ""
    response_model = ""
    response_tier = ""
    activity_response: Any = None
    chat_work_receipt: Any = None
    turn_refusals: list[Any] = []
    refusal_scope = serve_mod._CHAT_REFUSALS.set(turn_refusals)
    truncation_flag, truncation_scope = (
        deps.thinking_controls_mod.begin_reply_truncation()
    )
    serve_mod._ensure_refusal_observer()
    lifecycle = deps.lifecycle_mod.get()
    request_started_inner = time.monotonic()
    allow_control_routes = (
        model_operation != "responses" and serve_mod._uses_default_model_route(model)
    )
    allow_slash_command = model_operation != "responses" and prompt.lstrip().startswith("/")
    role_runs_tools = serve_mod._restricted_tool_role(context) is None

    handler._begin_chat_turn_telemetry(
        model=model, stream=stream, session_id=storage_session,
    )
    turn_stack = getattr(handler, "_turn_stack", None)
    turn_degradations = (
        turn_stack.enter_context(degradation_scope())
        if turn_stack is not None
        else []
    )
    turn_overflow = (
        turn_stack.enter_context(overflow_notice_scope())
        if turn_stack is not None
        else []
    )
    if turn_stack is not None:
        turn_stack.enter_context(
            chat_request_mode_scope(
                serve_mod.permission_policy,
                context,
                serve_mod._state_principal(context),
                session,
            )
        )
        turn_stack.enter_context(
            maybe_temperature_override(request_temperature, session_override=False)
        )
        if serve_mod._mode_principal(context) != serve_mod._state_principal(context):
            turn_stack.enter_context(
                serve_mod.permission_policy.mode_scope(
                    serve_mod._mode_principal(context), session,
                )
            )

    deps.reference_lookup_lanes_mod.reset_turn()
    tool_turn: Any = None
    client_slash_handoffs: list[Any] = []
    client_slash_records: list[Any] = []

    admission_rejected_cls = deps.lifecycle_mod.AdmissionRejected
    model_call_error_cls = runtime.ModelCallError

    try:
        with (
            deps.acquire_chat_slot(lifecycle),
            state.lock,
            contextlib.ExitStack() as early_scope,
        ):
            deps.chat_temperature_mod.store_session_field(
                apply_session_temperature,
                session_temperature,
                storage_session,
                context,
                serve_mod._state_principal,
            )
            deps.chat_effort_mod.store_session_field(
                apply_session_effort,
                session_reasoning_effort,
                storage_session,
                context,
                serve_mod._state_principal,
            )
            resolved_effort = deps.chat_effort_mod.resolve_turn_effort(
                request=request_reasoning_effort, session=storage_session,
            )
            early_scope.enter_context(
                deps.chat_effort_mod.effort_scope(resolved_effort.value)
            )
            active_persona_choice = (
                request_persona
                if request_persona is not None
                else (state.persona or deps.personas_mod.AUTO)
            )
            early_scope.enter_context(
                deps.personas_mod.persona_scope(active_persona_choice)
            )
            serve_mod._record_chat("user", prompt, state=state)
            spanda_http = deps.load_spanda_http()
            spanda_early = spanda_http.resolve_spanda_policy(
                serve_mod._SPANDA_CONFIG,
                handler.headers if serve_mod._developer_authorized(context) else None,
            )
            if (
                stream
                and structured_schema is None
                and not (spanda_early.active and spanda_early.block)
            ):
                handler._begin_early_stream(model)
                early_scope.callback(handler._pause_early_stream)
            with runtime.activity_tracker.response_span(
                "chat:%s" % (model or "sonder"),
                prompt,
                surface="http",
                model=model,
                session=storage_session,
                project=storage_project,
                reasoning_owner=serve_mod._reasoning_request_owner(context),
                feed_owner=serve_mod._feed_request_owner(context),
            ) as activity_response:
                if structured_schema is not None:
                    turn = serve_mod._run_structured_prompt(
                        prompt,
                        history,
                        model_selector,
                        structured_schema,
                        context_size=context_size,
                        metrics=lifecycle.metrics,
                        session=storage_session,
                        capture_request_id=handler._correlation(),
                        capture_turn_id=uuid.uuid4().hex,
                        capture_stream=stream,
                    )
                    content = turn.content
                    response_model = turn.resolved_model
                    response_tier = turn.resolved_tier
                elif allow_control_routes or allow_slash_command:
                    if client_slash_plan is not None:
                        reply, slash_iid = runtime._http_chat_tools().run_client_slash_turn(
                            handler,
                            client_slash_plan,
                            broker=serve_mod._CLIENT_TOOL_BROKER,
                            principal=serve_mod._state_principal(context),
                            session=session,
                            model=model,
                            stream=stream,
                            chunk_fn=serve_mod._chunk,
                            write_tool_chunk_fn=chat_write_tool_chunk,
                            slash_records=client_slash_records,
                            slash_handoffs=client_slash_handoffs,
                        )
                        if slash_iid:
                            response_iid = slash_iid
                        execution_routed = True
                    else:
                        reply = serve_mod._handle_slash(
                            prompt,
                            messages=chat_slash_messages(
                                messages, history, serve_mod._history_from_messages,
                            ),
                            state=state,
                            project=storage_project
                            or runtime._http_chat_tools().account_default_project(
                                context, serve_mod._state_principal(context),
                            ),
                            context=context,
                            idempotency_key=handler.headers.get("Idempotency-Key", ""),
                            workspace_project=project,
                            lane_session=session,
                            storage_session=storage_session,
                            selected_model=not allow_control_routes,
                        )
                if (
                    structured_schema is None
                    and allow_control_routes
                    and reply is None
                    and not context.get("account")
                    and not (
                        natural_model and natural_model["kind"] in ("fanout", "ensemble")
                    )
                ):
                    reply = serve_mod._handle_feedback(prompt, state=state)
                if (
                    structured_schema is None
                    and allow_control_routes
                    and reply is None
                    and serve_mod._developer_authorized(context)
                    and not (
                        natural_model and natural_model["kind"] in ("fanout", "ensemble")
                    )
                ):
                    reply = serve_mod._handle_intent(
                        prompt, messages=messages, state=state, context=context,
                    )
                if (
                    structured_schema is None
                    and allow_control_routes
                    and reply is None
                    and serve_mod._developer_authorized(context)
                    and not (
                        natural_model and natural_model["kind"] in ("fanout", "ensemble")
                    )
                ):
                    reply = serve_mod._handle_natural_command(
                        prompt,
                        messages=chat_slash_messages(
                            messages, history, serve_mod._history_from_messages,
                        ),
                        state=state,
                        project=storage_project
                        or runtime._http_chat_tools().account_default_project(
                            context, serve_mod._state_principal(context),
                        ),
                        context=context,
                        idempotency_key=handler.headers.get("Idempotency-Key", ""),
                        workspace_project=project,
                        lane_session=session,
                        storage_session=storage_session,
                    )
                if (
                    structured_schema is None
                    and reply is None
                    and natural_model
                    and natural_model["kind"] == "fanout"
                ):
                    reply = deps.run_natural_fanout(natural_model)
                if (
                    structured_schema is None
                    and reply is None
                    and natural_model
                    and natural_model["kind"] == "ensemble"
                ):
                    reply = deps.run_natural_ensemble(natural_model, storage_project)
                if (
                    structured_schema is None
                    and allow_control_routes
                    and role_runs_tools
                    and reply is None
                ):
                    reply = (
                        None if natural_model else runtime.route_computer_use(prompt)
                    ) or runtime.chat_web_response(
                        prompt,
                        history=history,
                        tier=model_selector or "code",
                        location_consent=location_consent,
                        location_hint=location_hint,
                        allow_server_location_lookup=(
                            location_consent
                            and bool(handler.client_address)
                            and serve_mod._is_loopback_host(handler.client_address[0])
                            and serve_mod._http_server_location_lookup_allowed(context)
                        ),
                        gateway_bound=True,
                    )
                    web_routed = reply is not None
                if structured_schema is None and reply is None:
                    explicit_image_model = (
                        deps.image_generation_mod.is_image_generation_model_name(model)
                    )
                    allow_media_route = allow_control_routes or serve_mod._model_to_tier(
                        model
                    ) in ("general", "code", "fast", "creative")
                    media_reply, media_model, media_tier = run_chat_media_route(
                        prompt=prompt,
                        model=model,
                        allow_media_route=allow_media_route,
                        explicit_image_model=explicit_image_model,
                        intents=serve_mod.intents,
                        image_generation=deps.image_generation_mod,
                        model3d_generation=deps.model3d_generation_mod,
                        logger=logger,
                    )
                    if media_reply is not None:
                        reply, response_model, response_tier = (
                            media_reply,
                            media_model,
                            media_tier,
                        )
                if structured_schema is None and reply is None and allow_control_routes:
                    work_session_ref = session or handler._correlation()
                    reply = serve_mod._handle_work_intent(
                        prompt,
                        project=serve_mod._work_project_for_request(
                            project, storage_project, prompt, context,
                        ),
                        authorized=serve_mod._developer_authorized(context),
                        context=context,
                        idempotency_key=handler.headers.get("Idempotency-Key", ""),
                        session_id=storage_session
                        or serve_mod._hosted_storage_id(
                            context, work_session_ref, "session",
                        ),
                        session_ref=work_session_ref,
                        correlation_id=handler._correlation(),
                        with_receipt=True,
                        tool_tier="",
                        record_transcript=bool(storage_session),
                    )
                    execution_routed = reply is not None
                if isinstance(reply, ChatWorkResult):
                    chat_work_receipt = reply.public_receipt()
                    reply = (
                        "Work outcome is unknown. Inspect the session receipt before retrying."
                        if reply.status == "unknown"
                        else reply.text or "Work was not started."
                    )
                if structured_schema is None and reply is not None:
                    if isinstance(reply, str) and "(/v1/generated-models/" in reply:
                        req_host = (handler.headers.get("Host") or "").strip()
                        if req_host:
                            req_scheme = (
                                "https"
                                if (
                                    hasattr(handler.connection, "getpeercert")
                                    or bool(
                                        getattr(
                                            handler.server,
                                            "tls_terminated_by_proxy",
                                            False,
                                        )
                                    )
                                    or os.environ.get(
                                        "SONDER_TLS_TERMINATED_BY_PROXY", ""
                                    )
                                    .strip()
                                    .lower()
                                    in ("1", "true", "yes")
                                )
                                else "http"
                            )
                            reply = deps.model3d_generation_mod.upgrade_markdown_download_links(
                                reply, origin=f"{req_scheme}://{req_host}",
                            )
                    content = reply
                elif structured_schema is None:
                    spanda_policy = spanda_http.resolve_spanda_policy(
                        serve_mod._SPANDA_CONFIG,
                        handler.headers
                        if serve_mod._developer_authorized(context)
                        else None,
                    )
                    handler._spanda_headers = None
                    prompt_control_guard = serve_mod._served_control_guard_for(
                        context, disabled=(model_operation == "responses"),
                    )
                    if spanda_policy.active:
                        base_temp = serve_temperature()
                        sample_temp = max(
                            float(spanda_policy.sample_temperature),
                            float(0.2 if base_temp is None else base_temp),
                            0.05,
                        )
                        samples: list[Any] = []
                        sample_turns: list[Any] = []
                        sample_lookups: dict[int, Any] = {}
                        with (
                            serve_temperature_override(sample_temp),
                            runtime.served_control_guard(prompt_control_guard),
                        ):
                            for _si in range(int(spanda_policy.k)):
                                sturn = serve_mod._run_prompt(
                                    prompt,
                                    history,
                                    model_selector,
                                    context_size=context_size,
                                    session="",
                                    project=storage_project,
                                    state=state,
                                    return_result=True,
                                    capture_request_id="",
                                    capture_turn_id="",
                                    capture_stream=False,
                                    cache_scope="",
                                    augment=not bool(context.get("account")),
                                    metrics=lifecycle.metrics,
                                )
                                samples.append(sturn.content)
                                sample_turns.append(sturn)
                                sample_lookups[id(sturn)] = (
                                    deps.reference_lookup_lanes_mod.take_turn()
                                )
                        spanda_eval = spanda_http.evaluate_samples(
                            samples,
                            alpha=spanda_policy.alpha,
                            threshold=spanda_policy.threshold,
                        )
                        handler._spanda_headers = spanda_http.response_headers(
                            spanda_eval
                        )
                        if spanda_policy.block and spanda_eval["uncertain"]:
                            handler._record_chat_completion_metric(
                                lifecycle,
                                "spanda_uncertain",
                                getattr(
                                    handler, "_request_started", request_started_inner,
                                ),
                            )
                            handler._send_json_payload(
                                spanda_http.uncertainty_error_body(
                                    spanda_eval,
                                    threshold=spanda_policy.threshold,
                                    correlation_id=handler._correlation(),
                                ),
                                status=spanda_http.BLOCK_STATUS,
                                headers=handler._spanda_headers,
                            )
                            return
                        dom = spanda_eval["dominant_answer"]
                        turn = next(t for t in sample_turns if t.content == dom)
                        deps.reference_lookup_lanes_mod.restore_turn(
                            sample_lookups.get(id(turn))
                        )
                        state.last_iid = turn.iid
                        state.last_run_source = turn.run_source
                        state.last_response = turn.content
                        turn = replace(turn, provider_capture=None)
                        content = dom
                        response_iid = turn.iid
                        response_reasoning = turn.thinking
                        response_model = turn.resolved_model
                        response_tier = turn.resolved_tier
                    else:
                        if stream:
                            handler._begin_early_stream(model)
                        tool_turn = runtime._http_chat_tools().build_http_chat_tool_turn(
                            server=runtime,
                            permission_policy=serve_mod.permission_policy,
                            handler=handler,
                            chunk_fn=serve_mod._chunk,
                            stream_module=serve_mod._chat_tool_stream,
                            model=model_selector or model,
                            project=project,
                            stream=stream,
                            authorized_tools=bool(
                                role_runs_tools
                                and serve_mod._developer_authorized(context)
                            ),
                            client_exec=client_exec,
                            client_broker=serve_mod._CLIENT_TOOL_BROKER,
                            client_principal=serve_mod._state_principal(context),
                            client_session=session,
                            account_scope=serve_mod._task_account_scope(context),
                        )
                        try:
                            with runtime.served_control_guard(prompt_control_guard):
                                turn = handler._run_streamable_prompt(
                                    prompt,
                                    history,
                                    model_selector,
                                    context_size=context_size,
                                    session=storage_session,
                                    project=storage_project,
                                    state=state,
                                    return_result=True,
                                    capture_request_id=handler._correlation(),
                                    capture_turn_id=uuid.uuid4().hex,
                                    capture_stream=stream,
                                    cache_scope=(
                                        ""
                                        if stream or tool_turn is not None
                                        else serve_mod._request_cache_scope(context)
                                    ),
                                    augment=not bool(context.get("account")),
                                    metrics=lifecycle.metrics,
                                    tool_turn=tool_turn,
                                )
                        finally:
                            handler._pause_early_stream()
                        content = turn.content
                        response_iid = turn.iid
                        response_reasoning = turn.thinking
                        response_model = turn.resolved_model
                        response_tier = turn.resolved_tier

            serve_mod._record_chat(
                "assistant",
                content,
                kind=(
                    "web"
                    if web_routed
                    else (
                        "execution"
                        if execution_routed
                        else ("slash" if reply is not None else "model")
                    )
                ),
                state=state,
            )
            if (
                turn is not None
                and reply is None
                and not web_routed
                and not execution_routed
            ):
                serve_mod._capture_live_session_turn(
                    session_id=storage_session,
                    prompt=prompt,
                    history=history,
                    model=response_model or model_selector,
                    content=content,
                    request_id=handler._correlation(),
                    turn_id=response_iid or uuid.uuid4().hex,
                    stream=stream,
                    provider_capture=turn.provider_capture,
                    selector=model_selector,
                    resolved_tier=response_tier,
                )
            elif (
                storage_session
                and reply is not None
                and not execution_routed
                and isinstance(content, str)
                and content.strip()
            ):
                _slash_cmd = (
                    prompt.lstrip().split(None, 1)[0].lower()
                    if isinstance(prompt, str) and prompt.lstrip().startswith("/")
                    else ""
                )
                _sensitive_slash = _slash_cmd in {
                    "/login", "/register", "/admin_login", "/admin_register",
                    "/admin_set_account", "/elevate", "/password", "/token",
                    "/apikey", "/secret", "/auth", "/account",
                }
                if not _sensitive_slash:
                    sync_turn_id = uuid.uuid4().hex
                    sync_lane = "web" if web_routed else ("slash" if allow_slash_command else "chat")
                    serve_mod._append_session_transcript_event(
                        storage_session,
                        "user.message",
                        prompt,
                        turn_id=sync_turn_id,
                        request_id=handler._correlation(),
                        extra={"lane": sync_lane},
                    )
                    serve_mod._append_session_transcript_event(
                        storage_session,
                        "model.response",
                        content,
                        turn_id=sync_turn_id,
                        request_id=handler._correlation(),
                        extra={"lane": sync_lane},
                    )
    except admission_rejected_cls as rejection:
        logger.error(
            f"request admission rejected: code={rejection.code!r}, "
            f"retryable={rejection.retryable}, correlation={handler._correlation()!r}"
        )
        handler._record_chat_completion_metric(
            lifecycle,
            rejection.code.lower(),
            getattr(handler, "_request_started", request_started_inner),
        )
        if handler._end_early_stream_with_error(
            str(rejection), "server_error", rejection.code, model,
        ):
            return
        handler._send_json_payload(
            deps.lifecycle_mod.error_envelope(
                rejection.code,
                str(rejection),
                handler._correlation(),
                retryable=rejection.retryable,
            ),
            status=rejection.status,
            headers={"Retry-After": "1"} if rejection.retryable else None,
        )
        return
    except model_call_error_cls as error:
        logger.error(
            f"model call error: kind={error.kind!r}, status={error.status}, "
            f"detail={error.detail!r}, correlation={handler._correlation()!r}"
        )
        handler._telemetry_error_kind = error.kind
        handler._record_chat_completion_metric(
            lifecycle,
            "model_error",
            getattr(handler, "_request_started", request_started_inner),
        )
        err_status = error.status or (
            502
            if error.kind in ("protocol", "empty_response", "request")
            else (504 if error.kind == "timeout" else 503)
        )
        if err_status == 408:
            err_status = 504
        if err_status not in (
            400, 401, 403, 404, 408, 413, 429, 500, 502, 503, 504,
        ):
            err_status = 502
        error_type = (
            "rate_limit_error"
            if err_status == 429
            else ("invalid_request_error" if 400 <= err_status < 500 else "server_error")
        )
        retry_after = "1"
        if error.retry_after_seconds is not None:
            retry_after = str(max(0, int(round(error.retry_after_seconds))))
        headers = {"Retry-After": retry_after} if err_status in (429, 503, 504) else None
        err_message = error.detail
        if getattr(error, "pool_refusal", None):
            err_message = error.pool_refusal
        if error.cloud and error.status == 429:
            err_message = runtime._format_model_call_error(error)
        if handler._end_early_stream_with_error(
            err_message, error_type, "MODEL_CALL_%s" % err_status, model,
        ):
            return
        handler._send_json_payload(
            {"error": {"message": err_message, "type": error_type}},
            status=err_status,
            headers=headers,
        )
        return
    except LiveSessionCaptureFailure:
        logger.error(
            f"durable session capture failed, correlation={handler._correlation()!r}",
            exc_info=True,
        )
        handler._record_chat_completion_metric(
            lifecycle,
            "session_capture_failed",
            getattr(handler, "_request_started", request_started_inner),
        )
        if handler._end_early_stream_with_error(
            "durable session capture unavailable",
            "server_error",
            "SESSION_CAPTURE_UNAVAILABLE",
            model,
        ):
            return
        handler._send_json_payload(
            {
                "error": {
                    "message": "durable session capture unavailable",
                    "type": "server_error",
                    "code": "SESSION_CAPTURE_UNAVAILABLE",
                }
            },
            status=503,
            headers={"Retry-After": "1"},
        )
        return
    except Exception as error:
        logger.error(
            f"unhandled exception in chat completion handler, correlation={handler._correlation()!r}",
            exc_info=True,
        )
        handler.log_error("request failed: %s", type(error).__name__)
        handler._record_chat_completion_metric(
            lifecycle,
            "error",
            getattr(handler, "_request_started", request_started_inner),
        )
        if handler._end_early_stream_with_error(
            "internal server error", "server_error", "INTERNAL_ERROR", model,
        ):
            return
        handler._send_json_payload(
            {
                "error": {
                    "message": "internal server error",
                    "type": "server_error",
                    "correlation_id": handler._correlation(),
                }
            },
            status=500,
        )
        return
    finally:
        serve_mod._CHAT_REFUSALS.reset(refusal_scope)
        deps.thinking_controls_mod.REPLY_TRUNCATION.reset(truncation_scope)
        serve_mod._release_http_conversation_state(state, state_pinned)

    request_started = getattr(handler, "_request_started", request_started_inner)
    handler._telemetry_error_reply = serve_mod._is_legacy_error_reply(content)
    if not serve_mod._reasoning_visible_to(context):
        response_reasoning = ""
    elapsed_ms = int((time.monotonic() - request_started) * 1000)
    receipt: dict[str, Any] = {
        "request_id": handler._correlation(),
        "elapsed_ms": max(0, elapsed_ms),
    }
    if response_model and response_tier:
        receipt["model"] = response_model
        receipt["tier"] = response_tier
    turn_cache = getattr(turn, "cache", "") if turn is not None else ""
    if turn_cache:
        receipt["cache"] = turn_cache
    if turn_degradations:
        receipt["degraded"] = list(turn_degradations)
    if chat_work_receipt is not None:
        receipt["chat_work"] = chat_work_receipt
    if turn is not None and reply is None and truncation_flag[0]:
        receipt["truncated"] = True
    receipt.update(reply_receipt_fields(reply))
    overflow_receipt = overflow_receipt_entry(turn_overflow)
    if overflow_receipt is not None:
        receipt["overflow"] = overflow_receipt
    refusal = turn_refusal_receipt(turn_refusals, context, serve_mod.permission_policy)
    if refusal is not None:
        receipt["refusal"] = refusal
    merge_client_tool_receipt(
        receipt,
        tool_turn=tool_turn,
        slash_records=client_slash_records,
        slash_handoffs=client_slash_handoffs,
    )
    content = deps.reference_lookup_lanes_mod.finish_http(content, receipt)
    if stream:
        streamed = handler._send_stream(
            content,
            model,
            iid=response_iid,
            elapsed_ms=elapsed_ms,
            receipt=receipt,
            usage=(
                deps.chat_formatting_mod.chat_usage(activity_response)
                if include_stream_usage
                else None
            ),
            activity_response=activity_response,
        )
        handler._record_chat_completion_metric(
            lifecycle,
            (
                "ok"
                if streamed is True
                else ("cancelled" if streamed is False else "stream_error")
            ),
            request_started,
        )
    else:
        try:
            if model_operation == "responses":
                handler._send_json(
                    facade.render_text(
                        model_operation,
                        content,
                        response_model or model,
                        receipt=receipt,
                    ),
                    elapsed_ms=elapsed_ms,
                )
            else:
                handler._send_json(
                    serve_mod._chat_completion_object(
                        content,
                        model,
                        iid=response_iid,
                        reasoning=response_reasoning,
                        elapsed_ms=elapsed_ms,
                        receipt=receipt,
                        activity_response=activity_response,
                    ),
                    elapsed_ms=elapsed_ms,
                )
        finally:
            handler._record_chat_completion_metric(lifecycle, "ok", request_started)


def send_stream(
    handler: Any,
    serve_mod: Any,
    content: Any,
    model: str,
    *,
    iid: str | None = None,
    elapsed_ms: int | float | None = None,
    receipt: Any = None,
    usage: Any = None,
    activity_response: Any = None,
) -> bool | None:
    """Send one complete SSE response."""
    serve_mod._serve_logger.debug(
        f"_send_stream: model={model!r}, content_len={len(content or '')}"
    )
    interaction_id = iid
    resolved_iid = iid or uuid.uuid4().hex[:12]
    activity = (
        serve_mod.server.activity_tracker.public_response(
            activity_response, include_detail=False,
        )
        if isinstance(activity_response, dict)
        else None
    )
    headers_sent = False
    early = getattr(handler, "_early_stream", None)
    if early is not None:
        if not early.stop():
            handler.close_connection = True
            return False
        headers_sent = True
    connection = getattr(handler, "connection", None)
    if connection is not None and early is None:
        try:
            connection.settimeout(serve_mod.STREAM_IDLE_TIMEOUT_SECONDS)
        except (AttributeError, OSError):
            pass
    frame_id, receipt = serve_mod._live_stream.frame_id_and_receipt(
        handler, interaction_id, resolved_iid, receipt,
    )
    try:
        if early is not None:
            result: bool | None = serve_mod.Handler._write_stream_body(
                handler,
                resolved_iid,
                model,
                content,
                elapsed_ms,
                receipt,
                usage,
                activity,
                lock=early.lock,
            )
            return result
        handler.send_response(200)
        handler._cors()
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-cache")
        if elapsed_ms is None:
            started = getattr(handler, "_request_started", None)
            elapsed_ms = int((time.monotonic() - started) * 1000) if started else 0
        handler.send_header("X-Sonder-Elapsed-Ms", str(max(0, int(elapsed_ms))))
        if getattr(handler, "_correlation_id", ""):
            handler.send_header("X-Sonder-Correlation-Id", handler._correlation_id)
        for spanda_name, spanda_value in (
            getattr(handler, "_spanda_headers", None) or {}
        ).items():
            handler.send_header(str(spanda_name), str(spanda_value))
        handler.send_header("Connection", "close")
        handler.close_connection = True
        handler.end_headers()
        headers_sent = True
        written: bool | None = serve_mod.Handler._write_stream_body(
            handler, resolved_iid, model, content, elapsed_ms, receipt, usage, activity,
        )
        return written
    except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
        handler.close_connection = True
        return False
    except Exception as error:
        serve_mod._serve_logger.error(
            f"SSE stream response failed after headers_sent={headers_sent}",
            exc_info=True,
        )
        handler.log_error("stream response failed: %s", type(error).__name__)
        if headers_sent:
            serve_mod.Handler._send_stream_terminal_error(handler, frame_id, model)
        handler.close_connection = True
        return None


def begin_early_stream(
    handler: Any,
    serve_mod: Any,
    *,
    model: str = "sonder",
    thread_factory: Callable[..., Any],
) -> Any:
    """Send SSE headers before generation and start keep-alive frames."""
    existing = getattr(handler, "_early_stream", None)
    if existing is not None:
        return existing
    connection = getattr(handler, "connection", None)
    if connection is not None:
        try:
            connection.settimeout(serve_mod.STREAM_IDLE_TIMEOUT_SECONDS)
        except (AttributeError, OSError):
            pass

    def write(frame: bytes) -> None:
        handler.wfile.write(frame)
        flush = getattr(handler.wfile, "flush", None)
        if callable(flush):
            flush()

    wait_state = serve_mod.server.ollama_pool.track_queue_wait()
    keepalive = SSEKeepAlive(
        write,
        serve_mod.STREAM_HEARTBEAT_SECONDS,
        thread_factory=thread_factory,
        status=wait_state.line,
    )
    handler._early_stream = keepalive
    handler._stream_model = model
    handler.close_connection = True
    try:
        handler.send_response(200)
        handler._cors()
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("X-Accel-Buffering", "no")
        started = getattr(handler, "_request_started", None)
        elapsed_ms = int((time.monotonic() - started) * 1000) if started else 0
        handler.send_header("X-Sonder-Elapsed-Ms", str(max(0, elapsed_ms)))
        if getattr(handler, "_correlation_id", ""):
            handler.send_header("X-Sonder-Correlation-Id", handler._correlation_id)
        handler.send_header("Connection", "close")
        handler.end_headers()
        write(KEEPALIVE_FRAME)
    except (OSError, ValueError):
        keepalive.client_gone = True
        return keepalive
    return keepalive.start()


def end_early_stream_with_error(
    handler: Any,
    serve_mod: Any,
    message: str,
    error_type: str,
    code: str,
    model: str,
) -> bool:
    """Deliver an error on an already-committed SSE response; else ``False``."""
    early = getattr(handler, "_early_stream", None)
    if early is None:
        return False
    handler.close_connection = True
    if early.stop():
        with early.lock:
            serve_mod.Handler._send_stream_terminal_error(
                handler,
                getattr(early, "completion_id", None) or uuid.uuid4().hex[:12],
                model or "sonder",
                message=message,
                error_type=error_type,
                code=code,
            )
    return True


def send_stream_terminal_error(
    handler: Any,
    iid: str,
    model: str,
    *,
    message: str | None = None,
    error_type: str = "server_error",
    code: str = "STREAM_INTERRUPTED",
) -> bool:
    """Best-effort terminal SSE error after an already-started stream."""
    payload = {
        "id": "chatcmpl-%s" % iid,
        "object": "error",
        "model": model,
        "error": {
            "message": message or "stream interrupted before completion",
            "type": error_type,
            "code": code,
        },
    }
    try:
        handler.wfile.write(("data: %s\n\n" % json.dumps(payload)).encode("utf-8"))
        handler.wfile.write(b"data: [DONE]\n\n")
        return True
    except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
        return False
    except Exception as error:
        handler.log_error("stream terminal error write failed: %s", type(error).__name__)
        return False


__all__ = [
    "ChatTurnDeps",
    "begin_early_stream",
    "end_early_stream_with_error",
    "handle_chat_completions",
    "send_stream",
    "send_stream_terminal_error",
]
