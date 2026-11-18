"""Composition-root helpers for Node1/pool worker binding, context hints, and ensemble scheduling."""
from __future__ import annotations

import contextlib
import contextvars
import json
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any


def _iter_gateways(root: object) -> list[Any]:
    if root is None:
        return []
    found: list[Any] = []
    visited: set[int] = set()
    stack: list[Any] = [root]
    while stack:
        obj = stack.pop()
        if obj is None or id(obj) in visited:
            continue
        visited.add(id(obj))
        found.append(obj)
        for attr in ("_inner", "inner", "_gateway", "gateway", "_primary", "primary", "_fallback", "fallback"):
            child = getattr(obj, attr, None)
            if child is not None and id(child) not in visited:
                stack.append(child)
        for attr in ("_providers", "providers"):
            mapping = getattr(obj, attr, None)
            if isinstance(mapping, Mapping):
                for child in mapping.values():
                    if child is not None and id(child) not in visited:
                        stack.append(child)
    return found


def attach_pool_to_gateway(gateway: object, pool: object) -> int:
    """Attach ``pool`` to every ``SonderInferenceGateway`` nested inside ``gateway``."""
    if pool is None:
        return 0
    attached = 0
    for candidate in _iter_gateways(gateway):
        attach = getattr(candidate, "attach_ollama_pool", None)
        if callable(attach):
            attach(pool)
            attached += 1
    return attached


def register_pool_worker(
    pool: Any,
    gateway: object,
    origin: str,
    *,
    allow_remote: bool | None = None,
    refresh: bool = True,
    invalidate_cache: Callable[[], object] | None = None,
) -> Any:
    """Register/refresh a peer Ollama worker across ``pool`` and attached gateways."""
    attach_pool_to_gateway(gateway, pool)
    snapshot = None
    for candidate in _iter_gateways(gateway):
        reg = getattr(candidate, "register_ollama_worker", None)
        if callable(reg):
            snapshot = reg(origin, allow_remote=allow_remote, ollama_pool=pool, refresh=refresh)
            break
    if snapshot is None:
        snapshot = pool.register_worker(origin, allow_remote=allow_remote, refresh=refresh)
    if callable(invalidate_cache):
        invalidate_cache()
    return snapshot


def pool_unified_status(pool: Any, gateway: object) -> dict[str, Any]:
    """Return pool capacity summary enriched with unified gateway worker status."""
    out: dict[str, Any] = {}
    cap_fn = getattr(pool, "pool_capacity_summary", None)
    if callable(cap_fn):
        cap = cap_fn()
        if isinstance(cap, dict):
            out["capacity_summary"] = cap
    for candidate in _iter_gateways(gateway):
        unified_fn = getattr(candidate, "unified_worker_status", None)
        if callable(unified_fn):
            unified = unified_fn(ollama_pool=pool)
            if isinstance(unified, dict):
                out["unified"] = unified
            break
    return out


def merge_pool_unified_status(page: Any, pool: Any, gateway: object) -> Any:
    """Merge ``pool_unified_status`` onto ``page`` and refresh ``serialized_bytes``."""
    if not isinstance(page, dict):
        return page
    extra = pool_unified_status(pool, gateway)
    if not extra:
        return page
    merged = {**page, **extra}
    if "serialized_bytes" in merged:
        for _ in range(3):
            merged["serialized_bytes"] = len(json.dumps(merged).encode("utf-8"))
    return merged


def fetch_ps_for_origin(
    pool: Any,
    origin: str,
    *,
    primary_origin: str,
    get_primary_ps: Callable[[], Any],
    admitted_get_fn: Callable[..., Any],
    read_bytes_fn: Callable[[Any], bytes],
) -> dict[str, Any]:
    """Fetch ``/api/ps`` from ``origin`` (primary uses caller's ``_get`` hook)."""
    norm_origin = str(origin or "").strip().rstrip("/")
    norm_primary = str(primary_origin or "").strip().rstrip("/")
    if not norm_origin or norm_origin.casefold() == norm_primary.casefold():
        data = get_primary_ps()
        return dict(data) if isinstance(data, Mapping) else {}
    if pool is None or not callable(getattr(pool, "open_url", None)):
        return {}
    try:
        with admitted_get_fn(pool, norm_origin, "/api/ps", timeout=5) as resp:
            parsed = json.loads(read_bytes_fn(resp).decode("utf-8"))
            return dict(parsed) if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def pool_context_hints(
    pool: Any,
    model: str,
    *,
    origin: str | None = None,
    default_origin: str = "",
) -> tuple[str, float | None, int | None, str | None, int | None]:
    """Return ``(selected_origin, active_billions, node_vram_bytes, node_kv_cache_type, worker_ctx)``."""
    fallback_origin = str(origin or default_origin or "").strip().rstrip("/")
    budget_fn = getattr(pool, "aggregate_context_budget", None)
    if not callable(budget_fn):
        return fallback_origin, None, None, None, None
    try:
        budget = budget_fn(model)
    except Exception:
        return fallback_origin, None, None, None, None
    if not isinstance(budget, Mapping):
        return fallback_origin, None, None, None, None
    selected = str(origin or budget.get("recommended_worker_origin") or fallback_origin or "").strip().rstrip("/")
    per_worker = budget.get("per_worker")
    worker_info = per_worker.get(selected) if isinstance(per_worker, Mapping) else None
    worker_map = worker_info if isinstance(worker_info, Mapping) else {}
    active_b = worker_map.get("active_billions")
    if active_b is None:
        active_b = budget.get("active_billions")
    vram_bytes = worker_map.get("vram_bytes")
    kv_type = worker_map.get("kv_cache_type")
    worker_ctx = worker_map.get("context")
    return (
        selected,
        float(active_b) if isinstance(active_b, (int, float)) and not isinstance(active_b, bool) else None,
        int(vram_bytes) if isinstance(vram_bytes, int) and not isinstance(vram_bytes, bool) else None,
        str(kv_type) if isinstance(kv_type, str) and kv_type.strip() else None,
        int(worker_ctx) if isinstance(worker_ctx, int) and not isinstance(worker_ctx, bool) else None,
    )


def selected_origin_for_model(pool: Any, model: str, *, default_origin: str = "") -> str:
    """Best known origin for ``model`` without triggering network probes."""
    selected, _ab, _vb, _kv, _ctx = pool_context_hints(pool, model, default_origin=default_origin)
    return selected or str(default_origin or "").strip().rstrip("/")


def strip_default_num_thread_for_remote(
    payload: Any,
    *,
    origin: str,
    is_loopback_fn: Callable[[str], bool],
    env_int_fn: Callable[[str], int | None],
) -> tuple[Any, bool]:
    """Strip coordinator-default ``num_thread`` when dispatching to a remote worker."""
    if not isinstance(payload, Mapping) or is_loopback_fn(origin):
        return payload, False
    if env_int_fn("SONDER_NUM_THREAD") is not None:
        return payload, False
    opts = payload.get("options")
    if not isinstance(opts, Mapping) or "num_thread" not in opts:
        return payload, False
    cleaned_opts = {k: v for k, v in opts.items() if k != "num_thread"}
    return {**payload, "options": cleaned_opts}, True


def group_ensemble_targets(
    targets: Sequence[tuple[str, str]],
    *,
    pool: Any,
    default_origin: str,
    is_cloud_fn: Callable[[str, str], bool],
    bridge_provider_fn: Callable[[str], str | None],
) -> list[tuple[str, list[tuple[int, str, str, bool]]]]:
    """Group ``[(tier, model)]`` by execution origin so distinct nodes run concurrently."""
    groups: dict[str, list[tuple[int, str, str, bool]]] = {}
    order: list[str] = []
    pool_enabled = bool(getattr(pool, "enabled", False))
    for idx, (tier, model) in enumerate(targets):
        bridge_provider = bridge_provider_fn(tier)
        is_cloud = is_cloud_fn(tier, model)
        is_local_ollama = (not is_cloud) and (bridge_provider is None)
        if not is_local_ollama:
            key = f"provider:{bridge_provider or 'cloud'}:{tier}"
        elif pool_enabled:
            key = selected_origin_for_model(pool, model, default_origin=default_origin)
        else:
            key = str(default_origin or "").strip().rstrip("/") or "__primary__"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append((idx, tier, model, is_local_ollama))
    return [(origin, groups[origin]) for origin in order]


def run_ensemble_groups(
    groups: Sequence[tuple[str, list[tuple[int, str, str, bool]]]],
    *,
    ask_member: Callable[[str, str, str], dict[str, Any]],
    unload_member: Callable[[str, str], None],
    pool_factory: Callable[..., Any],
    max_workers: int,
    synth_origin: str | None = None,
    synth_model: str | None = None,
) -> list[dict[str, Any]]:
    """Run distinct-origin ensemble groups concurrently; serialize within each node."""
    def run_group(origin: str, items: list[tuple[int, str, str, bool]]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        multi_same_node = len(items) > 1
        for pos, (idx, tier, model, is_local_ollama) in enumerate(items):
            item_result = ask_member(origin, tier, model)
            item_result["idx"] = idx
            results.append(item_result)
            if is_local_ollama:
                needs_same_node_unload = (
                    (pos < len(items) - 1)
                    or multi_same_node
                    or (
                        synth_origin is not None
                        and synth_origin == origin
                        and bool(synth_model)
                        and synth_model != model
                    )
                )
                if needs_same_node_unload:
                    unload_member(origin, model)
        return results

    ordered: list[dict[str, Any]] = []
    if len(groups) <= 1:
        for origin, items in groups:
            ordered.extend(run_group(origin, items))
    else:
        workers = max(1, min(len(groups), max_workers))
        with pool_factory(max_workers=workers) as executor:
            futures = [
                executor.submit(contextvars.copy_context().run, run_group, origin, items)
                for origin, items in groups
            ]
            for fut in futures:
                ordered.extend(fut.result())
    ordered.sort(key=lambda item: int(item.get("idx", 0)))
    return ordered


def auto_window_for_model(
    pool: Any,
    model: str,
    *,
    origin: str | None,
    default_origin: str,
    context_length: int | None,
    parameter_size: Any,
    cloud: bool,
    geometry: Any,
    kv_default: str,
    feedback: Any,
    memory: Any,
) -> int:
    """Compute node/MoE/residency-aware native context window for ``model``."""
    selected_origin, active_b, node_vram, node_kv, worker_ctx = (
        (str(origin or default_origin or "").strip().rstrip("/"), None, None, None, None)
        if cloud else pool_context_hints(pool, model, origin=origin, default_origin=default_origin)
    )
    eff_ctx = worker_ctx if (context_length is None and worker_ctx is not None) else context_length
    ceiling = None
    if feedback is not None and not cloud:
        kv_name = node_kv or kv_default
        feedback_origin = getattr(feedback, "origin", "")
        eff_origin = selected_origin if (feedback_origin and selected_origin) else None
        if eff_origin is not None:
            feedback.refresh(model, geometry=geometry, kv_type=kv_name, origin=eff_origin)
            ceiling = feedback.ceiling(model, origin=eff_origin)
        else:
            feedback.refresh(model, geometry=geometry, kv_type=kv_name)
            ceiling = feedback.ceiling(model)
    return int(memory.auto_window(
        model, eff_ctx, parameter_size, ceiling, cloud=cloud,
        active_billions=active_b, node_vram_bytes=node_vram, node_kv_cache_type=node_kv,
    ))


def execute_ensemble_poll(
    *,
    targets: Sequence[tuple[str, str]],
    pool: Any,
    default_origin: str,
    is_cloud_fn: Callable[[str, str], bool],
    bridge_provider_fn: Callable[[str], str | None],
    pin_origin_fn: Callable[[str], contextlib.AbstractContextManager[Any]],
    lookup: Any,
    make_tier_generate_fn: Callable[..., Any],
    num_predict: int,
    question: str,
    render_model_error_fn: Callable[[Exception], str],
    model_call_error_cls: type[BaseException],
    post_fn: Callable[..., Any],
    pool_factory: Callable[..., Any],
    max_workers: int,
) -> tuple[list[dict[str, Any]], list[tuple[str, str, str]]]:
    """Group targets by origin, execute concurrently across nodes, and unload only when needed."""
    groups = group_ensemble_targets(
        targets, pool=pool, default_origin=default_origin,
        is_cloud_fn=is_cloud_fn, bridge_provider_fn=bridge_provider_fn,
    )
    pool_enabled = bool(getattr(pool, "enabled", False))

    def ask_member(origin: str, tier: str, model: str) -> dict[str, Any]:
        started = time.monotonic()
        pin = pin_origin_fn(origin) if pool_enabled and not origin.startswith("provider:") else contextlib.nullcontext()
        try:
            with pin:
                target_num_ctx = lookup.num_ctx(tier, model)
                gen = make_tier_generate_fn(tier, model, "", 0.2, max(64, int(num_predict)), target_num_ctx, text_only=True)
                text = (lookup.ask(tier, gen, question) or "").strip()
        except Exception as error:
            if isinstance(error, model_call_error_cls) and getattr(error, "kind", None) == "cancelled":
                raise
            if isinstance(error, model_call_error_cls):
                return {"failure": (tier, model, render_model_error_fn(error))}
            return {"failure": (tier, model, str(error))}
        if text:
            served = (getattr(gen, "last_response_meta", None) or {}).get("served_model") or model
            return {"answer": {"tier": tier, "model": served, "answer": text, "elapsed_ms": int((time.monotonic() - started) * 1000)}}
        return {"failure": (tier, model, "empty response")}

    def unload_member(origin: str, model: str) -> None:
        try:
            pin = pin_origin_fn(origin) if pool_enabled and not origin.startswith("provider:") else contextlib.nullcontext()
            with pin:
                post_fn("/api/generate", {"model": model, "keep_alive": 0}, timeout=30)
        except Exception:
            pass

    outcomes = run_ensemble_groups(
        groups, ask_member=ask_member, unload_member=unload_member,
        pool_factory=pool_factory, max_workers=max_workers,
    )
    return (
        [row["answer"] for row in outcomes if "answer" in row],
        [row["failure"] for row in outcomes if "failure" in row],
    )


__all__ = [
    "attach_pool_to_gateway",
    "auto_window_for_model",
    "execute_ensemble_poll",
    "fetch_ps_for_origin",
    "group_ensemble_targets",
    "pool_context_hints",
    "pool_unified_status",
    "register_pool_worker",
    "run_ensemble_groups",
    "selected_origin_for_model",
    "strip_default_num_thread_for_remote",
]
