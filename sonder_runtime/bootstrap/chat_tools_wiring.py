"""Composition-root wiring for HTTP/MCP chat-tool turns, slash runners, and work-run refusals."""
from __future__ import annotations

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
    chat_tools_payload,
    preview_tool_arguments,
)


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


def start_default_prewarm(
    *,
    setting: bool,
    prewarm: Callable[..., Any],
    enabled: bool,
    thread_factory: Callable[..., Any],
) -> None:
    """Delegate startup prewarm when ``startup_prewarm`` is composed."""
    import importlib
    try:
        startup_prewarm = importlib.import_module("sonder_runtime.adapters.inference.startup_prewarm")
    except ImportError:
        return
    fn: Any = getattr(startup_prewarm, "start_default_model_prewarm", None)
    if callable(fn):
        fn(setting=setting, prewarm=prewarm, enabled=enabled, thread_factory=thread_factory)


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
    key = str(model or "").strip().casefold()
    extra_models = {
        item.strip().casefold()
        for item in str(os.environ.get("SONDER_CHAT_TOOL_MODELS", "") or "").split(",")
        if item.strip()
    }
    if key in extra_models:
        return True
    pool = getattr(server, "OLLAMA_POOL", None)
    snapshots_fn = getattr(pool, "snapshots", None)
    if callable(snapshots_fn):
        try:
            for snap in snapshots_fn():
                supports = getattr(snap, "supports_tools", None)
                if isinstance(supports, Mapping):
                    for name, flag in supports.items():
                        if str(name or "").strip().casefold() == key and flag is True:
                            return True
        except Exception:
            pass
    if bool(getattr(pool, "enabled", False)):
        return False
    try:
        records = server._runtime_installed_model_records()
        return bool(server._runtime_model_has_capability(model, "tools", records))
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
) -> ChatToolTurn | None:
    """Build a ``ChatToolTurn`` for an HTTP chat completion if enabled and capable."""
    if stream_module is not None:
        write_tool_chunk_fn = write_tool_chunk_fn or getattr(stream_module, "write_tool_chunk", None)
        write_agent_chunk_fn = write_agent_chunk_fn or getattr(stream_module, "write_agent_chunk", None)
        write_agent_board_chunk_fn = write_agent_board_chunk_fn or getattr(stream_module, "write_agent_board_chunk", None)
        wait_for_approval_fn = wait_for_approval_fn or getattr(stream_module, "wait_for_approval", None)
    flag = str(os.environ.get("SONDER_CHAT_TOOLS", "1")).strip().lower()
    if flag in {"0", "false", "no", "off"} or not authorized_tools:
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
        tools_payload=chat_tools_payload(allowed_names=admitted, depth=depth),
        backend=backend,
        permission_policy=permission_policy,
        blocked_tools=frozenset(getattr(server, "_AGENT_SYSTEM_OPERATOR_TOOLS", ())),
        on_tool_event=on_event if (stream and write_tool_chunk_fn is not None) else None,
        on_agent_event=on_agent_ev if (stream and write_agent_chunk_fn is not None) else None,
        on_board_event=on_board_ev if (stream and write_agent_board_chunk_fn is not None) else None,
        wait_for_approval=wait_hook if (stream and wait_for_approval_fn is not None) else None,
        role_refusal=role_tool_policy.agent_refusal,
        allow_system_tools=allow_system_tools,
    )


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
    "start_default_prewarm",
]
