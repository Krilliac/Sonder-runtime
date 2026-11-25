"""Bootstrap wiring for depth-1 agent delegation, Main+Node1 slot scheduling, and live peer boards."""
from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from sonder_runtime.adapters.filesystem import file_ops
from sonder_runtime.adapters.inference import gpu_admission
from sonder_runtime.adapters.inference.ollama_pool import local_agent_admission
from sonder_runtime.adapters.inference.pool_model_catalog import (
    PinResolution,
    PinSource,
    PinStatus,
)
from sonder_runtime.adapters.observability import activity_tracker
from sonder_runtime.adapters.security.permission_policy import PermissionPolicyProvider
from sonder_runtime.application.agents import delegation_runtime as _rt
from sonder_runtime.domain.agents.delegation import (
    DEFAULT_AGENT_PERSONAS,
    MAX_DELEGATED_TASKS,
    DelegationChildResult,
    DelegationTaskSpec,
    build_agent_drive_preamble,
    compact_text,
    extract_thinking_summary,
    format_delegation_report,
    normalize_batch_tasks,
    normalize_single_task,
    persona_for,
)
from sonder_runtime.domain.cloud_access import LEGACY_ERROR_PREFIX, has_legacy_error_prefix
from sonder_runtime.platform.runtime_threads import ThreadPoolExecutor

_logger = logging.getLogger(__name__)
_PROMPTS_PERSONAS_DIR = Path(__file__).resolve().parents[2] / "prompts" / "personas"
_SOFT_ERRORS = (RuntimeError, ValueError, TypeError, AttributeError, OSError, KeyError, LookupError)
_PARENT_CLOUD: contextvars.ContextVar[bool | None] = contextvars.ContextVar(
    "delegation_parent_cloud", default=None,
)


@contextlib.contextmanager
def _reset_cvar(cvar: Any, value: Any) -> Iterator[None]:
    if cvar is None or not hasattr(cvar, "set") or not hasattr(cvar, "reset"):
        yield
        return
    token = cvar.set(value)
    try:
        yield
    finally:
        cvar.reset(token)


@contextlib.contextmanager
def _child_delegation_isolation_scope() -> Iterator[None]:
    """Isolate per-turn/parent ContextVars so child runs cannot clobber parent state."""
    from sonder_runtime.application.chat import tool_loop as _tool_loop
    from sonder_runtime.interfaces.http import work_runs as _http_work_runs

    _pm = PermissionPolicyProvider()
    with contextlib.ExitStack() as stack:
        _preapproved_scope = _pm.clear_preapproved_scope()
        if isinstance(_preapproved_scope, contextlib.AbstractContextManager):
            stack.enter_context(_preapproved_scope)
        stack.enter_context(_tool_loop.bind_turn(None))
        stack.enter_context(_reset_cvar(getattr(_http_work_runs, "_CURRENT_RUN", None), ""))
        stack.enter_context(_reset_cvar(getattr(_pm, "_SPENT_APPROVAL", None), None))
        stack.enter_context(_reset_cvar(getattr(_pm, "_CALL_SPENDS", None), None))
        stack.enter_context(_reset_cvar(getattr(_pm, "_AGENT_APPROVAL_HOOK", None), None))
        stack.enter_context(_reset_cvar(getattr(_pm, "_ATTENDED_MODE_CHANGE", None), False))
        stack.enter_context(_reset_cvar(getattr(_pm, "_BYPASS_CONFIRMED", None), False))
        yield


def _spec_targets_cloud(server: Any, spec: DelegationTaskSpec) -> bool:
    """Return True if ``spec.tier`` or ``spec.model`` resolves to a cloud/hosted target."""
    tier_name = str(spec.tier or "").strip()
    model_name = str(spec.model or "").strip()
    if not tier_name and not model_name:
        return False

    tier_lc = tier_name.lower()
    model_lc = model_name.lower()
    if tier_lc.startswith("cloud") or tier_lc.endswith((":cloud", "-cloud")):
        return True
    if model_lc.endswith((":cloud", "-cloud")):
        return True

    is_cloud_tier = getattr(server, "_is_cloud_tier", None)
    if callable(is_cloud_tier):
        with contextlib.suppress(*_SOFT_ERRORS):
            if tier_name and bool(is_cloud_tier(tier_name, model_name or None)):
                return True
            if model_name and bool(is_cloud_tier(model_name, model_name)):
                return True

    is_cloud_model = getattr(server, "_is_cloud_model_name", None)
    if callable(is_cloud_model) and model_name:
        with contextlib.suppress(*_SOFT_ERRORS):
            if bool(is_cloud_model(model_name)):
                return True

    from sonder_runtime.application.chat import provider_bridge as _provider_bridge

    provider_for_tier = getattr(server, "_bridge_provider_for_tier", None)
    if callable(provider_for_tier):
        for candidate in (tier_name, model_name, f"model:{model_name}" if model_name else ""):
            if not candidate:
                continue
            with contextlib.suppress(*_SOFT_ERRORS):
                provider = provider_for_tier(candidate)
                if provider and _provider_bridge.is_hosted(provider):
                    return True

    serve_target = getattr(server, "_serve_target", None)
    if callable(serve_target):
        selector = model_name or tier_name
        with contextlib.suppress(*_SOFT_ERRORS):
            resolved = serve_target(selector, None)
            if isinstance(resolved, tuple) and len(resolved) >= 4:
                _res_model, res_cloud, _res_aug, res_label = resolved[:4]
                if bool(res_cloud) or res_label == "cloud-disabled":
                    return True
                if callable(provider_for_tier) and res_label:
                    provider = provider_for_tier(res_label)
                    if provider and _provider_bridge.is_hosted(provider):
                        return True
    return False


def _child_cloud_refusal(
    server: Any,
    spec: DelegationTaskSpec,
    *,
    parent_project: str,
) -> str:
    """Refuse cloud/hosted child targets when project-bound or parent is local-only."""
    if not _spec_targets_cloud(server, spec):
        return ""
    target = spec.model or spec.tier
    if str(parent_project or "").strip():
        return (
            f"{LEGACY_ERROR_PREFIX} HOST POLICY: project-bound delegation cannot route "
            f"child to cloud/hosted target {target!r}; select a local tier/model"
        )
    if _PARENT_CLOUD.get() is False:
        return (
            f"{LEGACY_ERROR_PREFIX} HOST POLICY: local-only parent agent cannot delegate "
            f"child to cloud/hosted target {target!r}; select a local tier/model"
        )
    return ""


def _resolve_spec_model_hint(server: Any, spec: DelegationTaskSpec) -> str:
    if spec.model:
        return spec.model.strip()
    tiers = getattr(server, "TIERS", None)
    tier_key = (spec.tier or "code").strip().lower()
    if isinstance(tiers, Mapping):
        mapped = str(tiers.get(tier_key) or "").strip()
        if mapped:
            return mapped
    return fleet_model_name()


def _resolve_persona_text(persona_file: str) -> str:
    if not persona_file:
        return ""
    candidate = _PROMPTS_PERSONAS_DIR / persona_file
    try:
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8").strip()[:500]
    except OSError:
        pass
    return ""


def _resolve_pool_slots(
    server: Any,
    *,
    requested: int,
    model: str = "",
    models: Sequence[str] = (),
) -> int:
    """Compute bounded concurrency across local capacity and Main+Node1 worker pool."""
    desired = max(1, min(MAX_DELEGATED_TASKS, int(requested or 1)))
    local_slots = 1
    orch = getattr(server, "master_orchestrator", None)
    cap_fn = getattr(orch, "capacity", None)
    if callable(cap_fn):
        try:
            cap = cap_fn(requested_agents=desired)
            if isinstance(cap, Mapping):
                local_slots = int(cap.get("worker_slots") or 1)
        except _SOFT_ERRORS as exc:
            _logger.debug("delegation capacity lookup failed: %s", exc)
    pool_slots = 0
    pool = getattr(server, "OLLAMA_POOL", None)
    pool_enabled = bool(getattr(pool, "enabled", False))
    summary_fn = getattr(pool, "pool_capacity_summary", None)
    if callable(summary_fn) and pool_enabled:
        try:
            summary = summary_fn()
            if isinstance(summary, Mapping):
                pool_slots = int(summary.get("total_slots") or 0)
        except _SOFT_ERRORS as exc:
            _logger.debug("delegation pool capacity lookup failed: %s", exc)
    if pool_enabled:
        candidate_models = [str(m).strip() for m in (*models, model) if str(m or "").strip()]
        if not candidate_models:
            tiers = getattr(server, "TIERS", None)
            default_model = str(tiers.get("code") or "").strip() if isinstance(tiers, Mapping) else ""
            default_model = default_model or fleet_model_name()
            if default_model:
                candidate_models = [default_model]
        unique_models = list(dict.fromkeys(candidate_models))
        carrier_totals: list[int] = []
        all_known = bool(unique_models)
        for item_model in unique_models:
            carriers = _model_carriers(pool, item_model)
            if carriers is None:
                all_known = False
                break
            total_for_model = int(carriers[1].get("total") or 0)
            if total_for_model < 1:
                all_known = False
                break
            carrier_totals.append(total_for_model)
        if all_known and carrier_totals:
            carrier_slots = sum(carrier_totals)
            if pool_slots > 0:
                carrier_slots = min(carrier_slots, pool_slots)
            return max(1, min(desired, carrier_slots, MAX_DELEGATED_TASKS))
    combined = max(local_slots, pool_slots, 1)
    return max(1, min(desired, combined, MAX_DELEGATED_TASKS))


def _reject_unsafe_child_path(raw: str) -> str:
    """Lexically refuse control chars and UNC/device/NT spellings before any FS call."""
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        raise PermissionError("refusing path containing ASCII control characters")
    text = raw.strip()
    if not text:
        return ""
    flat = text.replace("/", "\\")
    if flat in ("\\??", "\\\\??") or flat.startswith("\\??\\"):
        raise PermissionError("refusing a UNC, device, or NT-namespace path spelling")
    file_ops._reject_network_path(text)
    return text


def _is_within_root(path_text: str, root_text: str) -> bool:
    if not root_text or not path_text:
        return False
    try:
        clean_root = _reject_unsafe_child_path(root_text)
        clean_path = _reject_unsafe_child_path(path_text)
        if not clean_root or not clean_path:
            return False
        root_real = os.path.realpath(os.path.abspath(clean_root))
        cand_real = os.path.realpath(os.path.abspath(clean_path))
        return os.path.commonpath([root_real, cand_real]) == root_real
    except (OSError, ValueError, PermissionError):
        return False


def _bypass_root_active(server: Any) -> bool:
    pm = getattr(server, "permission_modes", None)
    fn = getattr(pm, "bypass_root_containment_active", None)
    return bool(fn()) if callable(fn) else False


def _resolve_child_project(
    server: Any,
    spec: DelegationTaskSpec,
    *,
    parent_project: str,
) -> tuple[str, str]:
    """Resolve child project/cwd without escaping an active parent project boundary."""
    raw_requested = str(spec.cwd or spec.project or "")
    if not raw_requested.strip():
        if raw_requested and any(ord(ch) < 32 or ord(ch) == 127 for ch in raw_requested):
            return "", f"{LEGACY_ERROR_PREFIX} delegated child project/cwd contains a control character"
        return parent_project, ""
    try:
        requested = _reject_unsafe_child_path(raw_requested)
    except PermissionError as exc:
        return "", f"{LEGACY_ERROR_PREFIX} delegated child project/cwd {raw_requested!r} refused: {exc}"
    if _bypass_root_active(server):
        return requested, ""
    if not parent_project:
        return "", (
            f"{LEGACY_ERROR_PREFIX} delegated child project/cwd {requested!r} "
            "requires an active parent project boundary"
        )
    try:
        clean_parent = _reject_unsafe_child_path(parent_project)
    except PermissionError as exc:
        return "", f"{LEGACY_ERROR_PREFIX} parent project boundary refused: {exc}"
    candidate = requested if os.path.isabs(requested) else os.path.join(clean_parent, requested)
    if not _is_within_root(candidate, clean_parent):
        msg = (
            f"{LEGACY_ERROR_PREFIX} delegated child project/cwd {requested!r} "
            f"exceeds parent project boundary {parent_project!r}"
        )
        return "", msg
    return os.path.realpath(os.path.abspath(candidate)), ""


def _forward_event_to_observers(board_item: Any, *, is_peer_message: bool) -> None:
    record_ev: Any = activity_tracker.record_event
    if is_peer_message:
        with contextlib.suppress(*_SOFT_ERRORS):
            record_ev(
                "agent_board_message",
                agent=board_item.agent,
                role=board_item.role,
                msg_kind=board_item.kind,
                text=board_item.text[:220],
                mentions=",".join(board_item.mentions),
            )
        with contextlib.suppress(*_SOFT_ERRORS):
            _rt.record_ephemeral_event(
                board_item.agent,
                f"[{board_item.kind}] {board_item.text[:200]}",
            )
        return
    with contextlib.suppress(*_SOFT_ERRORS):
        record_ev(
            "agent_progress",
            agent=board_item.agent_id,
            depth=board_item.depth,
            phase=board_item.phase,
            step=board_item.step,
            tool=board_item.tool,
            summary=(board_item.summary or board_item.thinking)[:200],
        )


def attach_board_observers(board: _rt.AgentProgressBoard) -> Callable[[], None]:
    """Subscribe activity_tracker + ephemeral snapshot observers to ``board``."""
    unsub_ev = board.subscribe(lambda ev: _forward_event_to_observers(ev, is_peer_message=False))
    unsub_peer = board.subscribe_board(lambda msg: _forward_event_to_observers(msg, is_peer_message=True))

    def detach() -> None:
        unsub_ev()
        unsub_peer()

    return detach


def build_runtime_identity_preamble(
    *,
    handle: str,
    role: str = "",
    personality: str = "",
    peers: Sequence[str] = (),
    depth: int = 0,
) -> str:
    """Build identity, voice, peer-board, and task-drive preamble for an active agent."""
    resolved_role, resolved_personality, voice, persona_file = persona_for(
        handle,
        explicit_role=role,
        explicit_personality=personality,
    )
    persona_text = _resolve_persona_text(persona_file)
    return build_agent_drive_preamble(
        handle=handle,
        role=resolved_role,
        personality=resolved_personality,
        voice_hint=voice,
        persona_text=persona_text,
        peers=peers,
        depth=depth,
    )


def _selected_origin_for_model(pool: Any, model: str, *, default_origin: str = "") -> str:
    from sonder_runtime.bootstrap.pool_wiring import selected_origin_for_model
    return selected_origin_for_model(pool, model, default_origin=default_origin)


def _run_one_child(
    server: Any,
    spec: DelegationTaskSpec,
    *,
    parent_agent_id: str,
    parent_project: str,
    parent_read_only: bool,
    peer_handles: Sequence[str],
    board: _rt.AgentProgressBoard,
    parent_allow_web: bool = True,
) -> DelegationChildResult:
    started = time.monotonic()
    effective_read_only = bool(parent_read_only) or bool(spec.read_only)
    child_project, boundary_err = _resolve_child_project(server, spec, parent_project=parent_project)
    tier = spec.tier or "code"
    resolved_model = spec.model or ""
    refusal_err = boundary_err or _child_cloud_refusal(server, spec, parent_project=parent_project)
    if refusal_err:
        board.emit(
            agent_id=spec.agent_id,
            parent_id=parent_agent_id,
            depth=1,
            phase="refused",
            tier=tier,
            model=resolved_model,
            ok=False,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            summary=refusal_err,
        )
        return DelegationChildResult(
            agent_id=spec.agent_id,
            task=spec.task,
            status="refused",
            ok=False,
            summary=refusal_err,
            output="",
            tier=tier,
            model=resolved_model,
            error=refusal_err,
            role=spec.role,
            personality=spec.personality,
        )

    _rt.register_ephemeral_child(
        spec.agent_id,
        spec.task,
        parent_id=parent_agent_id,
        tier=tier,
        model=resolved_model,
        project=child_project,
        activity=f"starting ({spec.role})",
    )
    board.emit(
        agent_id=spec.agent_id,
        parent_id=parent_agent_id,
        depth=1,
        phase="starting",
        tier=tier,
        model=resolved_model,
        max_steps=spec.max_steps,
        summary=f"starting ({spec.role}): {compact_text(spec.task, limit=150)}",
    )
    _rt.update_ephemeral_child(
        spec.agent_id,
        status="running",
        in_model_call=True,
        activity=f"running ({spec.role})",
    )

    preamble = build_runtime_identity_preamble(
        handle=spec.agent_id,
        role=spec.role,
        personality=spec.personality,
        peers=[p for p in peer_handles if p != spec.agent_id],
        depth=1,
    )
    thinking_section = ""
    if spec.role in {"coder", "security"} or str(tier or "").strip().lower() in {"code", "security"}:
        import sonder_runtime.bootstrap.thinking_ensemble as _thinking_ensemble
        from sonder_runtime.domain.routing.thinking_ensemble import format_thinking_brief

        child_lane = "security" if (spec.role == "security" or str(tier or "").strip().lower() == "security") else "code"
        delib = _thinking_ensemble.deliberate_for_turn(
            server,
            spec.task,
            tier=child_lane,
            model=resolved_model,
            cloud=False,
            lane=child_lane,
        )
        if delib is not None and delib.brief:
            thinking_section = "\n\n" + format_thinking_brief(
                delib.brief,
                thinking_tier=delib.tier,
                thinking_model=delib.model,
                lane=child_lane,
            )
            board.emit(
                agent_id=spec.agent_id,
                parent_id=parent_agent_id,
                depth=1,
                phase="thinking",
                tier=delib.tier,
                model=delib.model,
                elapsed_ms=delib.elapsed_ms,
                thinking=compact_text(delib.thinking or delib.brief, limit=240),
                summary=f"thinking brief from {delib.tier} ({delib.model})",
            )
    child_prompt = f"{preamble}{thinking_section}\n\n[DELEGATED TASK]\n{spec.task}"
    origin_info = {"origin": "", "model": resolved_model}

    pool = getattr(server, "OLLAMA_POOL", None)
    base_origin = str(getattr(server, "BASE", "") or "").strip().rstrip("/")
    if not origin_info["origin"] and resolved_model:
        origin_info["origin"] = _selected_origin_for_model(
            pool, resolved_model, default_origin=base_origin,
        )
    pool_route = getattr(pool, "local_agent_admission", None)
    if callable(pool_route) and bool(getattr(pool, "enabled", False)):
        route_ctx = pool_route(spec.agent_id)
    elif bool(getattr(pool, "enabled", False)):
        route_ctx = local_agent_admission(caller="agent")
    else:
        route_ctx = contextlib.nullcontext()

    with (
        _child_delegation_isolation_scope(),
        _rt.depth_scope(1),
        _rt.bind_board(board),
        _rt.agent_identity_scope(
            spec.agent_id,
            parent_id=parent_agent_id,
            role=spec.role,
            personality=spec.personality,
            model=resolved_model,
            origin=origin_info["origin"],
        ),
        route_ctx,
    ):
        try:
            call_kwargs: dict[str, Any] = {
                "tier": spec.model or tier,
                "max_steps": spec.max_steps,
                "allow_web": bool(parent_allow_web) and not effective_read_only,
                "read_only": effective_read_only,
                "project": child_project,
                "auto_checklist": False,
                "return_host_receipt": False,
            }
            output = str(server._agent_impl(child_prompt, **call_kwargs) or "")
            origin_info["model"] = _rt.current_agent_model() or origin_info["model"]
            last_route_fn = getattr(pool, "last_route", None)
            if callable(last_route_fn) and bool(getattr(pool, "enabled", False)):
                with contextlib.suppress(*_SOFT_ERRORS):
                    origin_info["origin"] = str(last_route_fn() or "")
            origin_info["origin"] = _rt.current_agent_origin() or origin_info["origin"]
            if not origin_info["origin"] and origin_info["model"]:
                origin_info["origin"] = _selected_origin_for_model(
                    pool, origin_info["model"], default_origin=base_origin,
                )
            elapsed_ms = int((time.monotonic() - started) * 1000)
            failed = has_legacy_error_prefix(output)
            status = "failed" if failed else "completed"
            summary = compact_text(output, limit=200)
            _rt.finish_ephemeral_child(
                spec.agent_id,
                status=status,
                activity=summary or status,
                error=summary if failed else "",
            )
            board.emit(
                agent_id=spec.agent_id,
                parent_id=parent_agent_id,
                depth=1,
                phase=status,
                tier=tier,
                model=origin_info["model"],
                origin=origin_info["origin"],
                ok=not failed,
                elapsed_ms=elapsed_ms,
                summary=summary or status,
            )
            return DelegationChildResult(
                agent_id=spec.agent_id,
                task=spec.task,
                status=status,
                ok=not failed,
                summary=summary,
                output=output,
                tier=tier,
                model=origin_info["model"],
                origin=origin_info["origin"],
                elapsed_ms=elapsed_ms,
                error=summary if failed else "",
                role=spec.role,
                personality=spec.personality,
            )
        except (*_SOFT_ERRORS, PermissionError) as exc:
            origin_info["model"] = _rt.current_agent_model() or origin_info["model"]
            origin_info["origin"] = _rt.current_agent_origin() or origin_info["origin"]
            elapsed_ms = int((time.monotonic() - started) * 1000)
            err = f"{type(exc).__name__}: {exc}"
            _rt.finish_ephemeral_child(
                spec.agent_id,
                status="failed",
                activity=err[:180],
                error=err,
            )
            board.emit(
                agent_id=spec.agent_id,
                parent_id=parent_agent_id,
                depth=1,
                phase="failed",
                tier=tier,
                model=origin_info["model"],
                origin=origin_info["origin"],
                ok=False,
                elapsed_ms=elapsed_ms,
                summary=err,
            )
            return DelegationChildResult(
                agent_id=spec.agent_id,
                task=spec.task,
                status="failed",
                ok=False,
                summary=err[:200],
                output="",
                tier=tier,
                model=origin_info["model"],
                origin=origin_info["origin"],
                elapsed_ms=elapsed_ms,
                error=err,
                role=spec.role,
                personality=spec.personality,
            )


def make_delegation_executor(server: Any) -> _rt.DelegationExecutor:
    """Create a hardware-bounded Main+Node1 delegation executor bound to ``server``."""

    def execute(
        specs: Sequence[DelegationTaskSpec],
        *,
        parent_agent_id: str,
        parent_project: str,
        parent_read_only: bool,
        board: _rt.AgentProgressBoard,
        parent_allow_web: bool = True,
    ) -> tuple[DelegationChildResult, ...]:
        if not _rt.can_delegate():
            raise PermissionError(_rt.depth_refusal_message("delegate_task"))
        if gpu_admission.held_ticket_active():
            raise RuntimeError(
                f"{LEGACY_ERROR_PREFIX} parent agent holds an active GPU admission ticket while delegating"
            )
        spec_list = list(specs)
        peer_handles = [parent_agent_id, *[item.agent_id for item in spec_list]]
        spec_models = [_resolve_spec_model_hint(server, item) for item in spec_list]
        slots = _resolve_pool_slots(server, requested=len(spec_list), models=spec_models)
        if len(spec_list) == 1 or slots == 1:
            results: list[DelegationChildResult] = []
            for item in spec_list:
                ctx = contextvars.copy_context()
                results.append(
                    ctx.run(
                        _run_one_child,
                        server,
                        item,
                        parent_agent_id=parent_agent_id,
                        parent_project=parent_project,
                        parent_read_only=parent_read_only,
                        peer_handles=peer_handles,
                        board=board,
                        parent_allow_web=parent_allow_web,
                    )
                )
            return tuple(results)

        ordered: list[DelegationChildResult | None] = [None] * len(spec_list)
        pool_factory: Any = ThreadPoolExecutor
        with pool_factory(max_workers=slots, thread_name_prefix="sonder-child") as executor:
            futures = []
            for idx, item in enumerate(spec_list):
                ctx = contextvars.copy_context()
                futures.append((
                    idx,
                    executor.submit(
                        ctx.run,
                        _run_one_child,
                        server,
                        item,
                        parent_agent_id=parent_agent_id,
                        parent_project=parent_project,
                        parent_read_only=parent_read_only,
                        peer_handles=peer_handles,
                        board=board,
                        parent_allow_web=parent_allow_web,
                    ),
                ))
            for idx, future in futures:
                ordered[idx] = future.result()
        return tuple(item for item in ordered if item is not None)

    return execute


def dispatch_delegation(
    server: Any,
    tool_name: str,
    args: Mapping[str, Any] | None,
    *,
    read_only: bool = False,
    repository_extra_roots: str = "",
    allow_web: bool = True,
) -> str:
    """Execute ``delegate_task`` or ``spawn_agents`` with strict depth-1 enforcement."""
    if not _rt.can_delegate():
        return _rt.depth_refusal_message(tool_name)
    try:
        specs = normalize_batch_tasks(args) if tool_name == "spawn_agents" else normalize_single_task(args)
    except (ValueError, TypeError) as exc:
        msg = f"{LEGACY_ERROR_PREFIX} {tool_name}: {exc}"
        return msg
    board = _rt.current_board() or _rt.AgentProgressBoard()
    owns_board = _rt.current_board() is None
    executor = _rt.current_delegation_executor() or make_delegation_executor(server)
    owns_exec = _rt.current_delegation_executor() is None
    parent_project = str(repository_extra_roots or "")
    if not owns_exec:
        for spec in specs:
            cloud_refusal = _child_cloud_refusal(server, spec, parent_project=parent_project)
            if cloud_refusal:
                return cloud_refusal
    detach = attach_board_observers(board) if owns_board else (lambda: None)
    try:
        with (
            _rt.bind_board(board) if owns_board else contextlib.nullcontext(),
            _rt.bind_delegation_executor(executor) if owns_exec else contextlib.nullcontext(),
        ):
            results = _rt.run_delegated_specs(
                specs,
                tool_name=tool_name,
                parent_project=parent_project,
                parent_read_only=bool(read_only),
                parent_allow_web=bool(allow_web),
            )
    except (PermissionError, RuntimeError) as exc:
        msg = str(exc) if str(exc).startswith(LEGACY_ERROR_PREFIX) else f"{LEGACY_ERROR_PREFIX} {exc}"
        return msg
    finally:
        detach()
    return format_delegation_report(
        results,
        events_summary=board.summary_lines(limit=10),
        board_summary=board.peer_summary_lines(limit=10),
    )


def dispatch_report_progress(args: Mapping[str, Any] | None) -> str:
    """Dispatch ``report_progress`` / ``post_status`` onto the active board."""
    data = args if isinstance(args, Mapping) else {}
    res = _rt.report_progress(
        str(data.get("summary") or data.get("status") or data.get("message") or ""),
        phase=str(data.get("phase") or "progress"),
        thinking=str(data.get("thinking") or ""),
        tool=str(data.get("tool") or ""),
    )
    return json.dumps(res, ensure_ascii=False)


def dispatch_post_to_board(args: Mapping[str, Any] | None) -> str:
    """Dispatch ``post_to_board`` onto the active shared peer board."""
    data = args if isinstance(args, Mapping) else {}
    res = _rt.post_to_board(
        str(data.get("text") or data.get("message") or ""),
        kind=str(data.get("kind") or "chat"),
    )
    return json.dumps(res, ensure_ascii=False)


def dispatch_read_board(args: Mapping[str, Any] | None) -> str:
    """Dispatch ``read_board`` against the active shared peer board."""
    data = args if isinstance(args, Mapping) else {}
    try:
        since_seq = int(data.get("since_seq") or 0)
    except (TypeError, ValueError):
        since_seq = 0
    try:
        limit = int(data.get("limit") or 20)
    except (TypeError, ValueError):
        limit = 20
    res = _rt.read_board_messages(
        since_seq=since_seq,
        limit=max(1, min(50, limit)),
        mentions_only=bool(data.get("mentions_only", False)),
    )
    return json.dumps(res, ensure_ascii=False)


def dispatch_agent_tool(
    server: Any,
    tool_name: str,
    args: Mapping[str, Any] | None,
    *,
    read_only: bool = False,
    repository_extra_roots: str = "",
    allow_web: bool = True,
) -> str:
    """Unified dispatch router for delegation, progress, and peer-board tools."""
    if tool_name in ("delegate_task", "spawn_agents"):
        return dispatch_delegation(
            server,
            tool_name,
            args,
            read_only=read_only,
            repository_extra_roots=repository_extra_roots,
            allow_web=allow_web,
        )
    if tool_name in ("report_progress", "post_status"):
        return dispatch_report_progress(args)
    if tool_name == "post_to_board":
        return dispatch_post_to_board(args)
    if tool_name == "read_board":
        return dispatch_read_board(args)
    msg = f"{LEGACY_ERROR_PREFIX} unknown delegation/board tool {tool_name!r}"
    return msg


class AgentImplBridge:
    """Lifecycle & step-observation bridge for ``server._agent_impl``."""

    def __init__(
        self,
        *,
        stack: contextlib.ExitStack,
        prompt: str,
        model: str,
        max_steps: int,
        cloud: bool,
        read_only: bool,
        project_scope: str,
    ) -> None:
        self._stack = stack
        self._closed = False
        try:
            self._model = model
            self._max_steps = max(1, int(max_steps or 1))
            self._cloud = cloud
            self._read_only = read_only
            self._project_scope = project_scope
            self._started = time.monotonic()
            self._board_cursor = 0
            self._depth = _rt.current_depth()
            self._prompt_preview = compact_text(prompt, limit=180)
            _rt.emit_agent_event(
                phase="starting",
                step=0,
                max_steps=self._max_steps,
                model=model,
                summary=self._prompt_preview,
            )
        except BaseException:
            self._closed = True
            stack.close()
            raise

    def maybe_preamble(self, *, transcript_chars: int = 0, num_ctx: int = 0) -> str:
        """Return compact coordinator/drive guidance when context budget comfortably permits."""
        if self._depth != 0 or self._read_only or self._cloud:
            return ""
        if num_ctx and int(num_ctx) <= 4096 and transcript_chars > 3800:
            return ""
        if transcript_chars > 4200 and num_ctx <= 0:
            return ""
        return build_runtime_identity_preamble(
            handle=_rt.current_agent_id(),
            role=_rt.current_agent_role(),
            personality=_rt.current_agent_personality(),
            depth=self._depth,
        )

    def observe_decision(self, step: int, raw: str, decision: Mapping[str, Any] | None) -> None:
        thinking = compact_text(
            extract_thinking_summary(raw)
            or (decision.get("reason") if isinstance(decision, Mapping) else ""),
            limit=280,
        )
        if thinking:
            _rt.emit_agent_event(
                phase="thinking",
                step=step,
                max_steps=self._max_steps,
                model=self._model,
                thinking=thinking,
                summary=thinking[:160],
            )

    def observe_tool_start(self, step: int, tool_name: str) -> None:
        _rt.emit_agent_event(
            phase="tool_call",
            step=step,
            max_steps=self._max_steps,
            tool=tool_name,
            model=self._model,
            summary=f"calling {tool_name}",
        )

    def observe_tool_done(self, step: int, tool_name: str, *, ok: bool, observation: str) -> None:
        _rt.emit_agent_event(
            phase="tool_result",
            step=step,
            max_steps=self._max_steps,
            tool=tool_name,
            ok=ok,
            model=self._model,
            summary=compact_text(observation, limit=180),
        )

    def drain_peer_updates(self) -> str:
        digest, self._board_cursor = _rt.unseen_peer_board_digest(self._board_cursor)
        if not digest:
            return ""
        return "[PEER BOARD UPDATES]\n" + digest

    def close(self, *, ok: bool = True, summary: str = "") -> None:
        if self._closed:
            return
        self._closed = True
        try:
            _rt.emit_agent_event(
                phase="completed" if ok else "failed",
                step=0,
                max_steps=self._max_steps,
                ok=ok,
                elapsed_ms=int((time.monotonic() - self._started) * 1000),
                model=self._model,
                summary=compact_text(summary, limit=180),
            )
        finally:
            self._stack.close()


def _find_sonder_inference_gateway(server: Any) -> Any:
    graph = getattr(server, "_APP_GRAPH", None)
    if graph is None:
        app_fn = getattr(server, "_application", None)
        if callable(app_fn):
            with contextlib.suppress(*_SOFT_ERRORS):
                graph = app_fn()
    root_gw = getattr(graph, "model_gateway", None)
    seen: set[int] = set()
    queue: list[Any] = [root_gw] if root_gw is not None else []
    while queue:
        cand = queue.pop(0)
        if cand is None or id(cand) in seen:
            continue
        seen.add(id(cand))
        if callable(getattr(cand, "unified_worker_status", None)):
            return cand
        for map_attr in ("providers", "_providers"):
            providers = getattr(cand, map_attr, None)
            if isinstance(providers, Mapping):
                queue.extend(providers.values())
        for attr in ("primary", "_primary", "fallback", "_fallback", "_gateway", "gateway"):
            nested = getattr(cand, attr, None)
            if nested is not None:
                queue.append(nested)
    return None


def resolve_sonder_inference_pin(
    server: Any,
    selector: str,
    *,
    fallback_pin: PinResolution,
) -> PinResolution:
    """Resolve direct model pins against healthy ``sonder-inference`` / pool workers."""
    wanted = str(selector or "").strip()
    if not wanted or fallback_pin.ok:
        return fallback_pin
    gw = _find_sonder_inference_gateway(server)
    if gw is None:
        return fallback_pin
    pool = getattr(server, "OLLAMA_POOL", None)
    try:
        status = gw.unified_worker_status(ollama_pool=pool)
    except _SOFT_ERRORS as exc:
        _logger.debug("unified_worker_status lookup failed: %s", exc)
        status = None
    candidates: list[str] = []
    if isinstance(status, Mapping):
        for raw_model in status.get("available_models") or ():
            text = str(raw_model or "").strip()
            if text:
                candidates.append(text)
    if not candidates:
        try:
            snap = gw.health()
            doc = getattr(snap, "document", None)
            if isinstance(doc, Mapping):
                for raw_model in doc.get("models") or ():
                    text = str(raw_model or "").strip()
                    if text:
                        candidates.append(text)
        except _SOFT_ERRORS as exc:
            _logger.debug("gateway health lookup failed: %s", exc)
    wanted_cf = wanted.casefold()
    matched = ""
    for item in candidates:
        if item.casefold() == wanted_cf:
            matched = item
            break
    if not matched and ":" not in wanted:
        prefix = wanted_cf + ":"
        for item in candidates:
            if item.casefold().startswith(prefix):
                matched = item
                break
    if not matched:
        return fallback_pin
    return PinResolution(
        PinStatus.RESOLVED,
        model=matched,
        record={"model": matched, "name": matched},
        source=PinSource.POOL,
    )


def _model_carriers(pool: Any, model: str | None) -> tuple[frozenset[str], dict[str, int]] | None:
    """Origins and slot counts of the pool workers carrying ``model``.

    ``None`` when no model was asked for, the pool is absent or disabled, or
    no worker advertises it yet: unknown capability must not read as zero.
    """
    if not model or pool is None or not bool(getattr(pool, "enabled", False)):
        return None
    try:
        counts = pool.model_slot_counts(model)
        origins = pool.origins_for_model(model)
    except _SOFT_ERRORS:
        return None
    if not counts or not origins:
        return None
    return frozenset(origins), counts


def fleet_model_name() -> str:
    """Model the fleet lane's tier runs under the runtime policy, or ``""``."""
    try:
        import sonder_runtime.adapters.runtime_policy as runtime_policy

        policy = runtime_policy.load(create=True)
        tier = runtime_policy.route_tier("fleet", policy=policy)
        return str((policy.get("local_models") or {}).get(tier) or "").strip()
    except Exception:
        return ""


def fleet_pool_route(server: Any) -> tuple[Any, str, dict[str, int]] | None:
    """``(pool, model, slot counts)`` when the pool serves the fleet model.

    The fleet tier routes through the Ollama worker pool when the pool is
    enabled and at least one healthy worker advertises the fleet model. Slot
    counts then cover only those workers, whichever hosts they are. ``None``
    means the local-host limits still apply.
    """
    pool = getattr(server, "OLLAMA_POOL", None)
    if pool is None or not bool(getattr(pool, "enabled", False)):
        return None
    model = fleet_model_name()
    if not model:
        return None
    try:
        counts = pool.model_slot_counts(model)
    except Exception:
        return None
    if not counts or int(counts.get("total") or 0) < 1:
        return None
    return pool, model, counts


def pool_capacity_note(
    route: tuple[Any, str, dict[str, int]] | None, requested: int, slots: int, bound_by: str,
) -> tuple[str, int, str]:
    """Apply the pool's real model capacity to a slot choice: ``(note, slots, bound_by)``.

    The explicit-cap and override branches of ``capacity`` do not read the
    slot limits, so the pool's capacity is applied to them here. The note is
    empty when the pool did not reduce the requested concurrency.
    """
    if route is None:
        return "", slots, bound_by
    cap = int(route[2]["total"])
    asked = slots if slots > cap else requested
    if slots > cap:
        slots, bound_by = cap, "pool_model_capacity"
    elif not (requested > slots and bound_by == "pool_model_capacity"):
        return "", slots, bound_by
    return (
        "%d agents were requested but the pool carries %s on %d worker(s) with %d "
        "slot(s) in total; running %d at a time, the rest in later waves"
        % (asked, route[1], int(route[2]["workers"]), cap, slots)
    ), slots, bound_by


def remote_cluster_parallel_slots(server: Any, model: str | None = None) -> int:
    """Return total healthy remote inflight/worker slots across Node1 and pool members.

    With ``model``, pool workers count only when they advertise it, so a
    worker that cannot serve the fleet's model adds no slots.
    """
    slots = 0
    gw = _find_sonder_inference_gateway(server)
    pool = getattr(server, "OLLAMA_POOL", None)
    carriers = _model_carriers(pool, model)
    if gw is not None:
        try:
            status = gw.unified_worker_status(ollama_pool=pool)
        except _SOFT_ERRORS:
            status = None
        if isinstance(status, Mapping):
            for row in status.get("workers") or ():
                if not isinstance(row, Mapping) or not row.get("healthy"):
                    continue
                src = str(row.get("source") or "")
                origin = str(row.get("origin") or row.get("base_url") or "")
                is_loopback = any(h in origin for h in ("127.0.0.1", "localhost", "[::1]"))
                if src == "sonder_inference_worker":
                    slots += max(1, int(row.get("max_inflight") or 1))
                elif src == "ollama_pool" and not is_loopback:
                    if carriers is not None and origin.rstrip("/") not in carriers[0]:
                        continue
                    slots += max(1, int(row.get("capacity") or 1))
            if slots > 0:
                return slots
    if carriers is not None:
        return int(carriers[1].get("remote_total") or 0)
    if pool is not None and bool(getattr(pool, "enabled", False)):
        try:
            summary = pool.pool_capacity_summary()
            if isinstance(summary, Mapping):
                total = int(summary.get("total_slots") or 0)
                if total > 1:
                    return total - 1
        except _SOFT_ERRORS as exc:
            _logger.debug("pool capacity summary failed: %s", exc)
    return slots


def effective_cluster_parallel(local_parallel: int, server: Any, model: str | None = None) -> int:
    """Combine local Ollama batch width with healthy remote pool/private worker slots.

    With ``model``, only workers that advertise it count: the local worker
    contributes nothing when it lacks the model, and remote pool workers
    count only when they carry it.
    """
    remote = remote_cluster_parallel_slots(server, model) if server is not None else 0
    carriers = _model_carriers(getattr(server, "OLLAMA_POOL", None), model)
    if carriers is not None and not int(carriers[1].get("local_total") or 0):
        return remote
    if remote > 0:
        return (int(local_parallel) if int(local_parallel) > 0 else 1) + remote
    return int(local_parallel)


def open_agent_run(
    server: Any,
    *,
    prompt: str,
    model: str,
    max_steps: int,
    cloud: bool,
    read_only: bool,
    project_scope: str = "",
) -> AgentImplBridge:
    """Bind board, executor, identity, and observers around one ``_agent_impl`` run."""
    stack = contextlib.ExitStack()
    try:
        board = _rt.current_board()
        if board is None:
            board = _rt.AgentProgressBoard()
            stack.enter_context(_rt.bind_board(board))
            detach = attach_board_observers(board)
            stack.callback(detach)
        if _rt.current_delegation_executor() is None and _rt.can_delegate():
            stack.enter_context(_rt.bind_delegation_executor(make_delegation_executor(server)))
        origin_hint = ""
        if not cloud and model:
            origin_hint = _selected_origin_for_model(
                getattr(server, "OLLAMA_POOL", None),
                model,
                default_origin=str(getattr(server, "BASE", "") or ""),
            )
        if _rt.current_depth() == 0:
            stack.enter_context(_reset_cvar(_PARENT_CLOUD, bool(cloud)))
            stack.enter_context(
                _rt.agent_identity_scope("coordinator", role="coordinator", model=model, origin=origin_hint)
            )
        else:
            _rt.note_agent_execution(model=model, origin=origin_hint)
    except BaseException:
        stack.close()
        raise
    bridge = AgentImplBridge(
        stack=stack,
        prompt=prompt,
        model=model,
        max_steps=max_steps,
        cloud=cloud,
        read_only=read_only,
        project_scope=project_scope,
    )
    scope = _OPEN_RUNS.get()
    if scope is not None and scope[0] == threading.get_ident():
        scope[1].append(bridge)
    return bridge


# Bridges opened inside the innermost ``agent_run_scope`` on its own thread.
_OPEN_RUNS: contextvars.ContextVar[tuple[int, list[AgentImplBridge]] | None] = contextvars.ContextVar(
    "delegation_open_agent_runs", default=None,
)


@contextlib.contextmanager
def agent_run_scope() -> Iterator[None]:
    """Close every bridge ``open_agent_run`` opened in this scope that its run left open.

    A run closes its bridge on its finish/fail paths.  A run that unwinds by an
    exception (``ensure_not_cancelled``, KeyboardInterrupt, a raising helper)
    does not, and its board, executor and identity bindings would stay set in
    the caller's context: the next run in that context would reuse the stale
    board and no terminal progress event would be sent.  On exit this closes
    them, newest first, in the thread and context that opened them: as failed
    when the body raised, else as completed.  ``close`` is idempotent, so a run
    that already closed its bridge is not reported twice.  Only bridges opened
    on the scope's own thread are tracked (a bridge's bindings can only be
    reset in the context that set them).
    """
    opened: list[AgentImplBridge] = []
    token = _OPEN_RUNS.set((threading.get_ident(), opened))
    ok = False
    try:
        yield
        ok = True
    finally:
        try:
            with contextlib.ExitStack() as cleanup:
                for bridge in opened:
                    cleanup.callback(
                        bridge.close, ok=ok, summary="" if ok else "agent run ended by an exception",
                    )
        finally:
            _OPEN_RUNS.reset(token)


__all__ = [
    "DEFAULT_AGENT_PERSONAS",
    "AgentImplBridge",
    "agent_run_scope",
    "attach_board_observers",
    "build_runtime_identity_preamble",
    "dispatch_agent_tool",
    "dispatch_delegation",
    "dispatch_post_to_board",
    "dispatch_read_board",
    "dispatch_report_progress",
    "effective_cluster_parallel",
    "make_delegation_executor",
    "open_agent_run",
    "remote_cluster_parallel_slots",
    "resolve_sonder_inference_pin",
]
