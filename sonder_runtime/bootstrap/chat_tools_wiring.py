"""Composition-root wiring for HTTP/MCP chat-tool turns, slash runners, and work-run refusals."""
from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from sonder_runtime.adapters.chat_run_backend import ChatRunBackend
from sonder_runtime.adapters.persistence import http_work_runs
from sonder_runtime.adapters.security import permission_receipts, role_tool_policy
from sonder_runtime.adapters.security import powershell_gate as _powershell_gate
from sonder_runtime.application.agents import delegation_runtime as _delegation
from sonder_runtime.application.chat.tool_loop import ChatToolTurn, build_tool_turn_manifest
from sonder_runtime.bootstrap import agent_help_tools as _agent_help_tools
from sonder_runtime.domain.agents.delegation import DELEGATION_TOOLS
from sonder_runtime.domain.chat_tools import (
    CORE_CHAT_TOOL_DEFINITIONS,
    ChatToolRecord,
    chat_tools_payload,
    preview_tool_arguments,
)
from sonder_runtime.domain import public_policy as _public_policy
from sonder_runtime.domain.client_tools import (
    CLIENT_DELEGATABLE_TOOLS,
    CLIENT_DELEGATABLE_TOOL_DEFINITIONS,
    CLIENT_EXEC_HEADER,
    TOOL_BACKEND_CLIENT,
    client_tool_risk,
    client_tools_payload,
    resolve_client_slash_command,
)
from sonder_runtime.interfaces.http.client_tools import DEFAULT_NON_STREAM_HANDOFF_TTL_SECONDS


def slash_run_arguments(
    cmd: str,
    sources: Callable[[], Any],
    extract: Callable[[Any], Any],
) -> dict[str, Any] | None:
    """Extract ``{code, language}`` for any ``/run*`` command (PowerShell or Python)."""
    if cmd not in _powershell_gate.RUN_COMMANDS:
        return None
    fn: Any = _powershell_gate.runnable_arguments
    result = fn(sources(), extract)
    return dict(result) if isinstance(result, Mapping) else None


def audit_mode_transition(
    previous: str,
    current: str,
    *,
    surface: str,
    actor: str,
    confirmed: bool = False,
) -> None:
    permission_receipts.emit_mode_transition(
        previous, current, surface=surface, actor=actor, confirmed=confirmed,
    )


def _model_supports_tools(server: Any, model: str) -> bool:
    candidates: list[str] = []
    raw_model = str(model or "").strip()
    if raw_model:
        candidates.append(raw_model)
    resolve_fn = getattr(server, "_serve_target", None)
    if callable(resolve_fn) and raw_model:
        try:
            resolved = resolve_fn(raw_model)
            if isinstance(resolved, str) and resolved.strip() and resolved.strip() not in candidates:
                candidates.append(resolved.strip())
        except Exception:
            pass
    keys = {c.casefold() for c in candidates if c}
    extra_models = {
        item.strip().casefold()
        for item in str(os.environ.get("SONDER_CHAT_TOOL_MODELS", "") or "").split(",")
        if item.strip()
    }
    if keys & extra_models:
        return True
    pool = getattr(server, "OLLAMA_POOL", None)
    snapshots_fn = getattr(pool, "snapshots", None)
    if callable(snapshots_fn):
        try:
            for snap in snapshots_fn():
                supports = getattr(snap, "supports_tools", None)
                if isinstance(supports, Mapping):
                    for name, flag in supports.items():
                        if str(name or "").strip().casefold() in keys and flag is True:
                            return True
        except Exception:
            pass
    app_graph = getattr(server, "_APP_GRAPH", None)
    if app_graph is None:
        app_fn = getattr(server, "_application", None)
        if callable(app_fn):
            try:
                app_graph = app_fn()
            except Exception:
                app_graph = None
    gateway = getattr(app_graph, "model_gateway", None) if app_graph is not None else None
    if gateway is not None:
        try:
            from sonder_runtime.adapters.legacy_chat_bridge import _sonder_inference_any_supports_tools

            if _sonder_inference_any_supports_tools(gateway, candidates):
                return True
        except Exception:
            pass
    try:
        records = server._runtime_installed_model_records()
        return any(bool(server._runtime_model_has_capability(c, "tools", records)) for c in candidates)
    except Exception:
        return False


def _container_running(server: Any = None) -> bool:
    try:
        runner = getattr(server, "code_runner", None) if server is not None else None
        fn: Any = getattr(runner, "container_enabled", None) if runner is not None else None
        return bool(fn()) if callable(fn) else False
    except Exception:
        return False


def _mcp_catalog(mcp: Any) -> Mapping[str, Any]:
    fn: Any = _agent_help_tools.catalog_for
    result = fn(mcp)
    return dict(result) if isinstance(result, Mapping) else {}


def build_http_chat_tool_turn(
    *,
    server: Any,
    permission_policy: Any,
    handler: Any,
    chunk_fn: Callable[..., str],
    write_tool_chunk_fn: Callable[..., bool] | None = None,
    wait_for_approval_fn: Callable[..., str] | None = None,
    model: str,
    project: str,
    stream: bool,
    authorized_tools: bool,
    write_agent_chunk_fn: Callable[..., bool] | None = None,
    write_agent_board_chunk_fn: Callable[..., bool] | None = None,
    stream_module: Any = None,
    wait_timeout_seconds: float = 120.0,
    allow_system_tools: bool = False,
    client_exec: bool = False,
    client_broker: Any = None,
    client_principal: str = "",
    client_session: str = "",
    write_sse_comment_fn: Callable[[], None] | None = None,
) -> ChatToolTurn | None:
    """Build a ``ChatToolTurn`` for an HTTP chat completion if enabled and capable."""
    if stream_module is not None:
        write_tool_chunk_fn = write_tool_chunk_fn or getattr(stream_module, "write_tool_chunk", None)
        write_agent_chunk_fn = write_agent_chunk_fn or getattr(stream_module, "write_agent_chunk", None)
        write_agent_board_chunk_fn = write_agent_board_chunk_fn or getattr(stream_module, "write_agent_board_chunk", None)
        wait_for_approval_fn = wait_for_approval_fn or getattr(stream_module, "wait_for_approval", None)
    flag = str(os.environ.get("SONDER_CHAT_TOOLS", "1")).strip().lower()
    if flag in {"0", "false", "no", "off"}:
        return None
    use_client_exec = bool(client_exec and client_broker is not None)
    if not authorized_tools and not use_client_exec:
        return None
    try:
        mode = str(permission_policy.current_mode() or "")
    except Exception:
        mode = ""
    if (not use_client_exec and mode == "plan") or not _model_supports_tools(server, model):
        return None
    depth = _delegation.current_depth()
    if use_client_exec and not authorized_tools:
        admitted = [item.name for item in CLIENT_DELEGATABLE_TOOL_DEFINITIONS]
        tools_payload = client_tools_payload()
        turn_client_tools = CLIENT_DELEGATABLE_TOOLS
    elif use_client_exec:
        admitted_core = [
            item.name for item in CORE_CHAT_TOOL_DEFINITIONS
            if not role_tool_policy.agent_refusal(item.name)
            and (depth == 0 or item.name not in DELEGATION_TOOLS)
        ]
        admitted = list(dict.fromkeys([*admitted_core, *(d.name for d in CLIENT_DELEGATABLE_TOOL_DEFINITIONS)]))
        core_non_client = [
            item.to_ollama_tool()
            for item in CORE_CHAT_TOOL_DEFINITIONS
            if item.name in admitted_core and item.name not in CLIENT_DELEGATABLE_TOOLS
        ]
        tools_payload = [*client_tools_payload(), *core_non_client]
        turn_client_tools = CLIENT_DELEGATABLE_TOOLS
    else:
        admitted = [
            item.name for item in CORE_CHAT_TOOL_DEFINITIONS
            if not role_tool_policy.agent_refusal(item.name)
            and (depth == 0 or item.name not in DELEGATION_TOOLS)
        ]
        tools_payload = chat_tools_payload(allowed_names=admitted, depth=depth)
        turn_client_tools = frozenset()
    if not admitted:
        return None
    container_active = _container_running(server) if authorized_tools else False
    manifest = build_tool_turn_manifest(
        admitted_tools=admitted,
        user_run_command=True,
        mode=("manual" if use_client_exec and not authorized_tools else (mode or "manual")),
        container_active=container_active,
    )
    backend = ChatRunBackend(
        dispatch_agent_tool=server._agent_dispatch,
        catalog_provider=(lambda: _mcp_catalog(server.mcp)) if authorized_tools else (lambda: {}),
        permission_policy=permission_policy,
        blocked_tools=frozenset(getattr(server, "_AGENT_SYSTEM_OPERATOR_TOOLS", ())),
        container_active=lambda: container_active,
        project_root=str(project or ""),
    )
    early = getattr(handler, "_early_stream", None)
    iid = str(getattr(early, "completion_id", "") or "") or uuid.uuid4().hex[:12]

    def on_event(payload: dict[str, Any]) -> None:
        if stream and write_tool_chunk_fn is not None:
            write_tool_chunk_fn(handler, chunk_fn, iid=iid, model=model, payload=payload)

    def on_agent_ev(payload: dict[str, Any]) -> None:
        if stream and write_agent_chunk_fn is not None:
            write_agent_chunk_fn(handler, chunk_fn, iid=iid, model=model, payload=payload)

    def on_board_ev(payload: dict[str, Any]) -> None:
        if stream and write_agent_board_chunk_fn is not None:
            write_agent_board_chunk_fn(handler, chunk_fn, iid=iid, model=model, payload=payload)

    handoffs: list[dict[str, Any]] = []

    def delegate_client(tool_name: str, args: dict[str, Any]) -> tuple[str, ChatToolRecord]:
        """Run one tool on the caller's PC.

        Streamed: wait in this turn for the client's posted result and give it
        to the model. Non-streamed: a one-way handoff. The turn ends with the
        call in ``sonder_receipt.client_tool_handoff``; the result endpoint
        stays open for ``DEFAULT_NON_STREAM_HANDOFF_TTL_SECONDS``, and nothing
        feeds the posted result back to the model in this turn or the next --
        a client that wants the model to see it sends it in its next message.
        """
        call = client_broker.create_call(
            principal=client_principal,
            session=client_session,
            tool=tool_name,
            args=args,
            risk=client_tool_risk(tool_name),
            timeout_seconds=wait_timeout_seconds if stream else DEFAULT_NON_STREAM_HANDOFF_TTL_SECONDS,
            mode="stream" if stream else "handoff",
        )
        if not stream:
            handoffs.append(call.handoff_metadata())
            record = ChatToolRecord(
                call_id=call.call_id,
                tool=tool_name,
                status="awaiting_client_result",
                backend=TOOL_BACKEND_CLIENT,
                reason="non-stream client execution handoff",
            )
            msg = (
                f"CLIENT TOOL HANDOFF ({call.call_id}): tool '{tool_name}' requires execution on "
                f"the client PC (result endpoint /v1/client-tools/{call.call_id}/result). "
                "Its result is not returned to you in this turn. Stop calling tools and "
                "report the pending client handoff."
            )
            return msg, record
        on_event(call.sse_event("awaiting_client_result"))
        outcome = client_broker.wait_for_result(
            call.call_id,
            timeout_seconds=wait_timeout_seconds,
            heartbeat_interval=5.0,
            on_wait=write_sse_comment_fn,
        )
        on_event(call.sse_event(outcome.status))
        if outcome.denied:
            obs = f"CLIENT TOOL DENIED: user declined {tool_name} ({call.call_id})."
            if outcome.error:
                obs += f" {outcome.error}"
        elif outcome.ok:
            obs = outcome.output or "(no output)"
        else:
            obs = f"CLIENT TOOL ERROR ({outcome.status}): {outcome.error or outcome.output or 'failed'}"
        record = ChatToolRecord(
            call_id=call.call_id,
            tool=tool_name,
            status=outcome.status,
            backend=TOOL_BACKEND_CLIENT,
            duration_ms=outcome.elapsed_ms,
            stdout_preview=outcome.output[:240] if outcome.ok else "",
            stderr_preview=outcome.error[:240] if not outcome.ok else "",
            reason=outcome.error[:200] if not outcome.ok else "",
        )
        return obs, record

    def wait_hook(tool_name: str, call_id: str, _decision: Any, preview: dict[str, Any]) -> str:
        ledger = permission_policy.approval_ledger()
        if ledger is None:
            return "expired"
        pending = None
        try:
            pending = ledger.resolve_call(call_id)
        except Exception:
            pending = None
        digest = (
            str(getattr(pending, "digest", "") or "")
            or str(server.permission_modes.call_digest(tool_name, preview.get("arguments") or {}))
        )
        if wait_for_approval_fn is None:
            return "expired"
        return wait_for_approval_fn(
            ledger,
            tool_name,
            digest,
            call_id,
            timeout_seconds=wait_timeout_seconds,
            is_aborted=(lambda: bool(early.client_gone)) if early is not None else None,
        )

    return ChatToolTurn(
        manifest=manifest,
        tools_payload=tools_payload,
        backend=backend,
        permission_policy=permission_policy,
        blocked_tools=frozenset(getattr(server, "_AGENT_SYSTEM_OPERATOR_TOOLS", ())),
        on_tool_event=on_event if (stream and write_tool_chunk_fn is not None) else None,
        on_agent_event=on_agent_ev if (stream and write_agent_chunk_fn is not None) else None,
        on_board_event=on_board_ev if (stream and write_agent_board_chunk_fn is not None) else None,
        wait_for_approval=wait_hook if (stream and wait_for_approval_fn is not None) else None,
        role_refusal=role_tool_policy.agent_refusal,
        allow_system_tools=allow_system_tools and authorized_tools,
        client_exec_tools=turn_client_tools,
        delegate_client_tool=delegate_client if use_client_exec else None,
        client_handoffs=handoffs,
    )


def account_default_project(context: Mapping[str, Any] | None, principal: str) -> str:
    """Return an isolated per-account fallback project namespace for slash commands."""
    if not (context or {}).get("account"):
        return ""
    material = "default-project\0" + str(principal or "")
    return "http-project-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def auth_response_extra_fields(
    account: Mapping[str, Any] | None,
    *,
    public_hosting: bool,
) -> dict[str, Any]:
    role = str((account or {}).get("role") or "user").strip().lower()
    return {
        "role": role,
        "public_hosting": bool(public_hosting),
        "client_exec_supported": True,
        "allowed_features": _public_policy.allowed_features_for_role(
            role,
            public_hosting=bool(public_hosting),
            client_exec_supported=True,
        ),
    }


def _feature_role(role: str, account: Any) -> str:
    """The role whose features a caller holds: an account's API key never holds
    admin (``admin_auth.require``), so an admin account's key gets a developer's."""
    if role == "admin" and isinstance(account, Mapping) and account.get("via_api_key"):
        return "developer"
    return role


def capabilities_payload(
    context: Mapping[str, Any] | None,
    *,
    auth_mode: str,
    public_hosting: bool,
    allow_registration: bool,
    require_account: bool,
    admin_authorized: bool,
) -> dict[str, Any]:
    account = (context or {}).get("account") if isinstance(context, Mapping) else None
    role = ""
    if isinstance(account, Mapping) and account.get("role"):
        role = str(account.get("role")).strip().lower()
    elif context and context.get("authorized"):
        role = "admin" if admin_authorized else "user"
    effective_role = _feature_role(role, account) or ("admin" if auth_mode == "local-open" else "user")
    payload: dict[str, Any] = {
        "object": "capabilities",
        "auth_mode": auth_mode,
        "public_hosting": bool(public_hosting),
        "registration_enabled": bool(allow_registration or public_hosting),
        "require_account": bool(require_account or auth_mode in ("account", "both")),
        "client_exec_supported": True,
        "client_exec_header": CLIENT_EXEC_HEADER,
        "client_result_endpoint": "/v1/client-tools/{call_id}/result",
        "client_delegatable_tools": sorted(CLIENT_DELEGATABLE_TOOLS),
        "client_owns_tool_policy": True,
        "host_bypass_allowed": bool(effective_role == "admin"),
        "allowed_features": _public_policy.allowed_features_for_role(
            effective_role,
            public_hosting=bool(public_hosting),
            client_exec_supported=True,
        ),
    }
    if role:
        payload["role"] = role
    if isinstance(account, Mapping) and account.get("username"):
        payload["username"] = str(account["username"])
    return payload


def whoami_payload(
    context: Mapping[str, Any] | None,
    *,
    auth_mode: str,
    public_hosting: bool,
    admin_authorized: bool,
) -> dict[str, Any]:
    account = dict((context or {}).get("account") or {})
    role = str(account.get("role") or ("admin" if admin_authorized else "user")).strip().lower()
    feature_role = _feature_role(role, account)
    return {
        "ok": True,
        "username": str(account.get("username") or ""),
        "role": role,
        "auth_mode": auth_mode,
        "public_hosting": bool(public_hosting),
        "client_exec_supported": True,
        "host_bypass_allowed": bool(feature_role == "admin"),
        "allowed_features": _public_policy.allowed_features_for_role(
            feature_role,
            public_hosting=bool(public_hosting),
            client_exec_supported=True,
        ),
        "account": account,
    }


def negotiate_client_exec(
    req: Mapping[str, Any],
    headers: Any,
    *,
    public_hosting: bool,
    restricted_role: str | None,
) -> tuple[bool, str | None]:
    """Validate and resolve ``(client_exec, validation_error)`` for a chat request."""
    raw_client_tools = req.get("client_tools")
    if raw_client_tools is not None and not isinstance(raw_client_tools, bool):
        return False, "client_tools must be a boolean"
    header_val = str(headers.get(CLIENT_EXEC_HEADER, "") or "").strip().lower()
    if raw_client_tools is False or header_val in ("0", "false", "no", "off"):
        return False, None
    if raw_client_tools is True or header_val in ("1", "true", "yes", "on"):
        return True, None
    return bool(public_hosting and restricted_role is not None), None


def resolve_client_slash_for_request(
    prompt: str,
    *,
    client_exec: bool,
    model_operation: str,
) -> Any:
    if not client_exec or model_operation == "responses":
        return None
    return resolve_client_slash_command(prompt)


def run_client_slash_turn(
    handler: Any,
    plan: Any,
    *,
    broker: Any,
    principal: str,
    session: str,
    model: str,
    stream: bool,
    chunk_fn: Callable[..., str],
    write_tool_chunk_fn: Callable[..., bool],
    slash_records: list[dict[str, Any]],
    slash_handoffs: list[dict[str, Any]],
) -> tuple[str, str | None]:
    """Execute one client-delegatable slash command via ``ClientToolBroker``."""
    call = broker.create_call(
        principal=principal,
        session=session,
        tool=plan.tool,
        args=plan.args,
        risk=plan.risk,
        mode="stream" if stream else "handoff",
    )
    if not stream:
        slash_handoffs.append(call.handoff_metadata())
        reply = (
            f"Client execution required for /{plan.tool} "
            f"(call_id {call.call_id}). Submit result to "
            f"/v1/client-tools/{call.call_id}/result."
        )
        return reply, None
    response_iid: str | None = None
    handler._begin_early_stream(model)
    try:
        early = getattr(handler, "_early_stream", None)
        iid = str(getattr(early, "completion_id", "") or "") or uuid.uuid4().hex[:12]
        write_tool_chunk_fn(
            handler, chunk_fn, iid=iid, model=model,
            payload=call.sse_event("awaiting_client_result"),
        )
        outcome = broker.wait_for_result(call.call_id)
        write_tool_chunk_fn(
            handler, chunk_fn, iid=iid, model=model,
            payload=call.sse_event(outcome.status),
        )
        response_iid = iid
    finally:
        handler._pause_early_stream()
    if outcome.denied:
        reply = f"Client declined {plan.tool} ({call.call_id})."
        if outcome.error:
            reply += f" {outcome.error}"
    elif outcome.ok:
        reply = outcome.output or "(no output)"
    else:
        reply = f"Client tool error ({outcome.status}): {outcome.error or outcome.output or 'failed'}"
    slash_records.append({
        "call_id": call.call_id,
        "tool": plan.tool,
        "status": outcome.status,
        "backend": TOOL_BACKEND_CLIENT,
        "duration_ms": outcome.elapsed_ms,
    })
    return reply, response_iid


def sonder_status_payload(
    *,
    server: Any,
    context: Mapping[str, Any],
    caps: Mapping[str, Any],
    public_hosting: bool,
    admin_authorized: bool,
    detail_allowed: bool,
    memory_receiver_configured: bool,
    app_control_binding: Any,
    state_home: str,
    deployment_payload: Any,
    build_operational_capabilities: Callable[..., Any],
    narrate: Callable[[dict[str, Any]], Any],
) -> Any:
    """Build the ``GET /v1/sonder/status`` payload for restricted or admin callers."""
    account = context.get("account") or {}
    models = [
        {"id": "sonder", "owned_by": "local"},
        *[
            {
                "id": tier_name,
                "owned_by": "cloud" if server._is_cloud_tier(tier_name, model) else "local",
            }
            for tier_name, model in server.available_tiers().items()
        ],
    ]
    if not admin_authorized:
        return {
            "status": "restricted",
            "account": account,
            "public_hosting": bool(public_hosting),
            "client_exec_supported": True,
            "allowed_features": caps["allowed_features"],
            "capabilities": caps,
            "models": models,
            "operational": {
                "available": False,
                "reason": "host-wide diagnostics require administrator authorization",
            },
        }
    agents = server.master_orchestrator.snapshot()
    activity_source = server.activity_tracker.snapshot()
    activity = server.activity_tracker.public_snapshot(
        activity_source, include_detail=detail_allowed,
    )
    from sonder_runtime.bootstrap.app import default_app

    application: Any = default_app()
    memory_replication_service = getattr(application, "memory_replication", None)
    memory_replication_status = (
        memory_replication_service.status()
        if memory_replication_service is not None
        else None
    )
    fixed_peer_artifact_copy_configured = False
    artifact_availability = getattr(application, "_artifact_mobility_available", None)
    if callable(artifact_availability):
        try:
            fixed_peer_artifact_copy_configured = bool(artifact_availability())
        except Exception:
            fixed_peer_artifact_copy_configured = False
    try:
        inference_pool_status = server.OLLAMA_POOL.summary()
    except Exception:
        inference_pool_status = None
    payload: dict[str, Any] = {
        "status": server.status(),
        "providers": list(application.provider_health_data()),
        "stats": server.sonder_stats(),
        "learn_tiers": server.learn_tiers(),
        "improvements": server.system_improvement_report(),
        "context": server.context_health_data(),
        "context_policy": server.context_policy.policy(server.SESSION_NUM_CTX),
        "agents": agents,
        "execution": server.execution_status_data(
            agents, activity_source, include_detail=detail_allowed,
        ),
        "autopilot": server.autopilot_controller.snapshot(),
        "runtime_policy": server.runtime_policy_data(),
        "selfmod": server.selfmod.status_data(),
        "mcp_runtime": server.mcp_runtime_data(),
        "npu_fallback": server.npu_fallback_status_data(),
        "learning_health": server.learning_health_data(),
        "memory_replication": memory_replication_status,
        "operational_capabilities": build_operational_capabilities(
            config=getattr(application, "config", None),
            inference_pool_status=inference_pool_status,
            memory_receiver_configured=memory_receiver_configured,
            memory_replication_status=memory_replication_status,
            managed_work_configured=(
                app_control_binding is not None
                and getattr(app_control_binding, "_work_binding", None) is not None
            ),
            fixed_peer_artifact_copy_configured=fixed_peer_artifact_copy_configured,
        ),
        "activity": activity,
        "db_path": getattr(server, "_DB_PATH", ""),
        "state_home": state_home,
        "deployment": deployment_payload,
        "account": account,
        "public_hosting": bool(public_hosting),
        "client_exec_supported": True,
        "allowed_features": caps["allowed_features"],
        "capabilities": caps,
        "models": models,
    }
    return narrate(payload)


def maybe_open_mcp_chat_tool_turn(
    *,
    server: Any,
    permission_policy: Any,
    model: str,
    cloud: bool,
    project: str,
) -> ChatToolTurn | None:
    """Open a ``ChatToolTurn`` for MCP ``sonder()`` when model supports native tools."""
    flag = str(os.environ.get("SONDER_CHAT_TOOLS", "1")).strip().lower()
    if flag in {"0", "false", "no", "off"} or cloud:
        return None
    try:
        mode = str(permission_policy.current_mode() or "")
    except Exception:
        mode = ""
    if mode == "plan" or not _model_supports_tools(server, model):
        return None
    depth = _delegation.current_depth()
    admitted = [
        item.name for item in CORE_CHAT_TOOL_DEFINITIONS
        if not role_tool_policy.agent_refusal(item.name)
        and (depth == 0 or item.name not in DELEGATION_TOOLS)
    ]
    if not admitted:
        return None
    container_active = _container_running(server)
    manifest = build_tool_turn_manifest(
        admitted_tools=admitted,
        user_run_command=True,
        mode=mode or "manual",
        container_active=container_active,
    )
    backend = ChatRunBackend(
        dispatch_agent_tool=server._agent_dispatch,
        catalog_provider=lambda: _mcp_catalog(server.mcp),
        permission_policy=permission_policy,
        blocked_tools=frozenset(getattr(server, "_AGENT_SYSTEM_OPERATOR_TOOLS", ())),
        container_active=lambda: container_active,
        project_root=str(project or ""),
    )
    return ChatToolTurn(
        manifest=manifest,
        tools_payload=chat_tools_payload(allowed_names=admitted, depth=depth),
        backend=backend,
        permission_policy=permission_policy,
        blocked_tools=frozenset(getattr(server, "_AGENT_SYSTEM_OPERATOR_TOOLS", ())),
        role_refusal=role_tool_policy.agent_refusal,
    )


def record_work_run_pending_refusal(
    run_id: str,
    decision: Any,
    tool_name: str,
    arguments: Mapping[str, Any] | None,
    *,
    refusal_receipt_fn: Callable[..., dict[str, Any] | None],
    mode_label: str = "",
    modes_allowing: tuple[str, ...] | list[str] = (),
) -> None:
    """Record ``pending_refusal`` onto the active ``http_work_runs`` row if ``run_id`` is set."""
    if not run_id:
        return
    receipt = refusal_receipt_fn(decision, mode_label=mode_label, modes_allowing=modes_allowing)
    if receipt is None:
        return
    preview = preview_tool_arguments(tool_name, arguments or {})
    receipt.update({
        "detail": preview["detail"],
        "detail_truncated": preview["detail_truncated"],
        "detail_chars": preview["detail_chars"],
        "bidi_warning": preview["bidi_warning"],
        "bidi_chars": preview["bidi_chars"],
    })
    try:
        http_work_runs.set_narration(run_id, pending_refusal=receipt)
    except Exception:
        pass


def clear_work_run_pending_refusal(run_id: str) -> None:
    """Clear any ``pending_refusal`` once an operator approves or a run resumes."""
    if not run_id:
        return
    try:
        http_work_runs.set_narration(run_id, clear_pending_refusal=True)
    except Exception:
        pass


__all__ = [
    "audit_mode_transition",
    "build_http_chat_tool_turn",
    "clear_work_run_pending_refusal",
    "maybe_open_mcp_chat_tool_turn",
    "record_work_run_pending_refusal",
    "slash_run_arguments",
]
