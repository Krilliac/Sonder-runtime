"""Local GPU residency: keep the primary chat model loaded; spot contention.

Two operator concerns on a single consumer GPU (16 GB class):

* **Reloads between turns (G9).**  ``SONDER_KEEP_ALIVE`` (default ``2m``)
  unloads an idle Ollama model, and reloading a ~27B model costs ~20 s at
  the start of the next turn.  With ``SONDER_KEEP_PRIMARY_RESIDENT=1`` the
  primary chat model (the model the default chat route resolves to) is
  requested with ``keep_alive: -1`` so Ollama keeps it loaded; every other
  model keeps ``SONDER_KEEP_ALIVE``.  Off by default: pinning is only safe
  when that model is the only local model sharing the GPU, which
  :func:`gpu_sharing_findings` (the ``sonder_inference_gpu`` doctor check)
  reports on.  A pinned model stays loaded until Ollama restarts or
  ``ollama stop <model>`` -- including after the primary tier changes.
* **Silent spill (G10).**  A 15+ GB ``llama-server`` behind a local Sonder
  Inference leaves no room on a 16 GB card: any other local model that
  Ollama loads onto the GPU (a tier bound to Ollama, or the embedder) pushes
  the server's weights or KV into shared system memory, and decode drops by
  2-15x with no error.  :func:`gpu_sharing_findings` names each contender;
  nothing changes a default silently.
"""
from __future__ import annotations

import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any

from ...domain import ollama_policy
from ...domain.common.errors import Forbidden, SonderError
from ...domain.inference_profiles import (
    NODE_ABSENT,
    NODE_ERROR,
    NODE_OK,
    InferenceNodeSnapshot,
    format_node_gpu_pool_summary,
    parse_inference_node_document,
)
from ...domain.model_routing import is_cloud_model_name
from ..model_list_consumers import runtime_configuration
from ..model_tool_policy import shipped_floor
from ..provider_bindings import PROVIDER_TIERS
from .exchange_transport import _bounded, trust_scope
from .log_safe import redact_url, safe_origin

ENV_KEEP_PRIMARY_RESIDENT = "SONDER_KEEP_PRIMARY_RESIDENT"
ENV_EMBED_ON_CPU = "SONDER_EMBED_ON_CPU"
ENV_EMBED_BASE_URL = "SONDER_EMBED_BASE_URL"
# Ollama's own switch that restricts its GPU discovery to one backend library.
# ``cpu`` makes the daemon find no GPU at all (0.34.4, 2026-09-30: `ollama ps`
# reports 100% CPU for every model and nvidia-smi does not move), so nothing
# it loads can share the card with a local Sonder Inference server.
ENV_OLLAMA_LLM_LIBRARY = "OLLAMA_LLM_LIBRARY"
RESIDENT_KEEP_ALIVE = -1  # a JSON number: Ollama reads a negative value as "forever"
_ON = frozenset({"1", "true", "yes", "on"})


def _enabled(env: Mapping[str, str], name: str) -> bool:
    return str(env.get(name, "") or "").strip().lower() in _ON


def local_ollama_pinned_to_cpu(env: Mapping[str, str] | None = None) -> bool:
    """Whether OLLAMA_LLM_LIBRARY=cpu keeps the local daemon off every GPU.

    Read from this process's environment, which on a workstation is the same
    User environment the daemon inherits; a daemon started before the value
    was set still has to be restarted, which the doctor detail says.
    """
    source = os.environ if env is None else env
    return str(source.get(ENV_OLLAMA_LLM_LIBRARY, "") or "").strip().lower() == "cpu"


def _embedder_loads_locally(env: Mapping[str, str]) -> bool:
    """False when SONDER_EMBED_BASE_URL sends embeddings to another host.

    A dedicated endpoint on a loopback address is still this machine's GPU.
    The opt-in local fallback forces ``num_gpu: 0``, so it never counts.
    """
    raw = str(env.get(ENV_EMBED_BASE_URL, "") or "").strip()
    if not raw:
        return True
    try:
        host = urllib.parse.urlparse(ollama_policy.normalize(raw)).hostname
    except ValueError:
        return True
    if not host:  # an unparsable origin is not provably remote
        return True
    return ollama_policy.is_loopback(raw)


def keep_primary_resident(env: Mapping[str, str] | None = None) -> bool:
    return _enabled(os.environ if env is None else env, ENV_KEEP_PRIMARY_RESIDENT)


def keep_alive_for(
    model: object, default: object, *, primary: Callable[[], object],
    env: Mapping[str, str] | None = None,
) -> object:
    """``keep_alive`` for one Ollama request naming ``model``.

    ``default`` unless the flag is on and ``model`` is the primary chat model
    (``primary()`` is only called when the flag is on; any failure keeps the
    default).
    """
    if not keep_primary_resident(env) or not isinstance(model, str) or not model:
        return default
    try:
        resolved = primary()
    except Exception:  # noqa: BLE001 - residency is an optimisation, never a failure
        return default
    return RESIDENT_KEEP_ALIVE if resolved == model else default


def _local_ollama(env: Mapping[str, str]) -> bool:
    try:
        return ollama_policy.is_loopback(str(env.get("OLLAMA_HOST", "") or "") or None)
    except Exception:  # noqa: BLE001 - an unparsable host is not provably local
        return False


def local_ollama_tier_models(env: Mapping[str, str], bindings) -> dict[str, str]:
    """``{tier: model}`` for tiers a local Ollama serves on this machine."""
    if not _local_ollama(env):
        return {}
    tiers = dict(runtime_configuration(env, floor=shipped_floor()).tier_map())
    found: dict[str, str] = {}
    for tier in PROVIDER_TIERS:
        model = str(tiers.get(tier, "") or "").strip()
        if (bindings.tier_providers.get(tier) == "ollama" and model
                and not is_cloud_model_name(model)):
            found[tier] = model
    return found


def gpu_sharing_findings(env: Mapping[str, str], bindings, *, inference_local: bool) -> list[str]:
    """Plain statements of what would share the GPU with a local Inference.

    Nothing local Ollama loads can share the card while it is pinned to the
    CPU library (``OLLAMA_LLM_LIBRARY=cpu``); see :func:`gpu_sharing_report`
    for the statement the doctor prints in that case.
    """
    if local_ollama_pinned_to_cpu(env):
        return []
    return _contenders(env, bindings, inference_local=inference_local)


def gpu_sharing_report(env: Mapping[str, str], bindings, *, inference_local: bool) -> dict:
    """``{"findings": [...], "note": str}`` for the doctor.

    ``findings`` is what :func:`gpu_sharing_findings` returns. ``note`` is a
    single statement when OLLAMA_LLM_LIBRARY=cpu suppressed contenders: it
    names them, so a daemon that was never restarted after the variable was
    set is still visible to the operator.
    """
    contenders = _contenders(env, bindings, inference_local=inference_local)
    if not local_ollama_pinned_to_cpu(env):
        return {"findings": contenders, "note": ""}
    note = "local Ollama is pinned to the CPU library (%s=cpu)" % ENV_OLLAMA_LLM_LIBRARY
    if contenders:
        note += (
            ", so these cannot take the GPU (restart the daemon if it predates "
            "the setting): %s" % "; ".join(contenders)
        )
    return {"findings": [], "note": note}


def _contenders(env: Mapping[str, str], bindings, *, inference_local: bool) -> list[str]:
    findings: list[str] = []
    inference_tiers = sorted(
        tier for tier in PROVIDER_TIERS
        if bindings.tier_providers.get(tier) == "sonder_inference"
    )
    ollama_models = local_ollama_tier_models(env, bindings)
    if inference_tiers and inference_local:
        if ollama_models:
            findings.append(
                "tier(s) %s run local Ollama model(s) %s that load on the GPU "
                "beside the Sonder Inference server (tiers %s)"
                % (", ".join(sorted(ollama_models)),
                   ", ".join(sorted(set(ollama_models.values()))),
                   ", ".join(inference_tiers))
            )
        if (bindings.embedding_provider == "ollama" and _local_ollama(env)
                and _embedder_loads_locally(env)
                and not _enabled(env, ENV_EMBED_ON_CPU)):
            findings.append(
                "the Ollama embedder loads on the GPU beside the Sonder "
                "Inference server; set %s=1 to keep it on the CPU" % ENV_EMBED_ON_CPU
            )
    if keep_primary_resident(env) and len(set(ollama_models.values())) > 1:
        findings.append(
            "%s=1 pins the primary chat model while other local Ollama models "
            "(%s) can also load; they will contend with it"
            % (ENV_KEEP_PRIMARY_RESIDENT, ", ".join(sorted(set(ollama_models.values()))))
        )
    return findings


NODE_ENDPOINT_PATH = "/v1/sonder/node"
NODE_PROBE_TIMEOUT_SECONDS = 2.0
DEFAULT_NODE_TTL_SECONDS = 60.0
NODE_BODY_LIMIT = 262_144


class NodeSnapshotCache:
    """Thread-safe per-endpoint TTL cache for ``/v1/sonder/node`` observations."""

    def __init__(self, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._refresh = threading.Lock()
        self._entries: dict[str, InferenceNodeSnapshot] = {}

    def cached(self, base_url: str) -> InferenceNodeSnapshot | None:
        """Return the last cached observation for ``base_url`` without I/O."""
        with self._lock:
            return self._entries.get(base_url)

    def _fresh(self, base_url: str, ttl: float, requested_at: float) -> InferenceNodeSnapshot | None:
        cached = self.cached(base_url)
        if cached is None:
            return None
        if cached.checked_monotonic > requested_at or requested_at - cached.checked_monotonic < ttl:
            return cached
        return None

    def get(
        self,
        base_url: str,
        fetch: Callable[[], InferenceNodeSnapshot],
        *,
        ttl: float = DEFAULT_NODE_TTL_SECONDS,
        refresh: bool = False,
    ) -> InferenceNodeSnapshot:
        """Return the cached node snapshot while fresh, else run one single-flight ``fetch``."""
        requested_at = self._monotonic()
        if not refresh:
            cached = self._fresh(base_url, ttl, requested_at)
            if cached is not None:
                return cached
        with self._refresh:
            if not refresh:
                cached = self._fresh(base_url, ttl, requested_at)
                if cached is not None:
                    return cached
            fetched = fetch()
            snapshot = replace(fetched, checked_monotonic=self._monotonic())
            with self._lock:
                self._entries[base_url] = snapshot
            return snapshot

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


_NODE_CACHE = NodeSnapshotCache()


def shared_node_cache() -> NodeSnapshotCache:
    """Process-wide ``/v1/sonder/node`` cache shared by doctor/node diagnostics."""
    return _NODE_CACHE


def cached_inference_node(base_url: str) -> InferenceNodeSnapshot | None:
    """Return the last cached ``/v1/sonder/node`` snapshot for ``base_url`` (no I/O)."""
    return _NODE_CACHE.cached(base_url)


def _display_origin(settings: Any) -> str:
    display = getattr(settings, "display_base_url", None)
    if isinstance(display, str) and display.strip():
        return safe_origin(display) or redact_url(display)
    base = str(getattr(settings, "base_url", "") or "")
    return safe_origin(base) or redact_url(base)


def _node_error_snapshot(display_url: str, detail: str) -> InferenceNodeSnapshot:
    return InferenceNodeSnapshot(
        base_url=display_url,
        state=NODE_ERROR,
        detail=_bounded(detail),
    )


def _node_absent_snapshot(display_url: str, detail: str) -> InferenceNodeSnapshot:
    return InferenceNodeSnapshot(
        base_url=display_url,
        state=NODE_ABSENT,
        detail=_bounded(detail),
    )


def _read_node_get(
    settings: Any,
    *,
    timeout: float,
    get_transport: Callable[[str, dict[str, str], float], tuple[int, bytes]] | None = None,
) -> tuple[int, bytes]:
    url = str(settings.base_url).rstrip("/") + NODE_ENDPOINT_PATH
    headers: dict[str, str] = {"Accept": "application/json"}
    api_key = str(getattr(settings, "api_key", "") or "").strip()
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    bounded_timeout = max(0.05, min(float(timeout), 6.0))
    ca_bundle = str(getattr(settings, "ca_bundle", "") or "")
    with trust_scope(ca_bundle):
        if get_transport is not None:
            return get_transport(url, headers, bounded_timeout)
        from .sonder_inference_gateway import direct_get_transport

        return direct_get_transport(url, headers, bounded_timeout)


def _fetch_node_for_settings(
    settings: Any,
    *,
    timeout: float = NODE_PROBE_TIMEOUT_SECONDS,
    get_transport: Callable[[str, dict[str, str], float], tuple[int, bytes]] | None = None,
) -> InferenceNodeSnapshot:
    """Perform one policy-checked, TLS-verified, bounded GET of ``/v1/sonder/node``."""
    from .sonder_inference_gateway import check_endpoint_policy

    display_url = _display_origin(settings)
    try:
        check_endpoint_policy(settings)
    except Exception as exc:  # noqa: BLE001 - diagnostics degrade cleanly
        return _node_error_snapshot(
            display_url,
            str(exc) if isinstance(exc, SonderError) else type(exc).__name__,
        )
    try:
        status, body = _read_node_get(settings, timeout=timeout, get_transport=get_transport)
    except ssl.SSLCertVerificationError:
        return _node_error_snapshot(display_url, "node probe failed: TLS certificate verification failed")
    except ssl.SSLError:
        return _node_error_snapshot(display_url, "node probe failed: TLS handshake failed")
    except urllib.error.HTTPError as exc:
        code = int(exc.code or 0)
        if code in (404, 405, 501):
            return _node_absent_snapshot(display_url, "no %s (HTTP %d)" % (NODE_ENDPOINT_PATH, code))
        return _node_error_snapshot(display_url, "%s returned HTTP %d" % (NODE_ENDPOINT_PATH, code))
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, ssl.SSLCertVerificationError):
            return _node_error_snapshot(display_url, "node probe failed: TLS certificate verification failed")
        if isinstance(reason, ssl.SSLError):
            return _node_error_snapshot(display_url, "node probe failed: TLS handshake failed")
        if isinstance(reason, TimeoutError):
            return _node_error_snapshot(display_url, "node probe timed out")
        return _node_error_snapshot(display_url, "node probe failed: endpoint unreachable")
    except TimeoutError:
        return _node_error_snapshot(display_url, "node probe timed out")
    except ConnectionRefusedError:
        return _node_error_snapshot(display_url, "node probe failed: connection refused")
    except SonderError as exc:
        return _node_error_snapshot(display_url, "node probe failed: %s" % exc)
    except Exception as exc:  # noqa: BLE001 - diagnostics never raise
        return _node_error_snapshot(display_url, "node probe failed: %s" % type(exc).__name__)

    if status in (404, 405, 501):
        return _node_absent_snapshot(display_url, "no %s (HTTP %d)" % (NODE_ENDPOINT_PATH, status))
    if status in (401, 403):
        return _node_error_snapshot(display_url, "%s rejected request (HTTP %d)" % (NODE_ENDPOINT_PATH, status))
    if status != 200:
        return _node_error_snapshot(display_url, "%s returned HTTP %d" % (NODE_ENDPOINT_PATH, status))
    if not isinstance(body, bytes) or len(body) > NODE_BODY_LIMIT:
        return _node_error_snapshot(display_url, "%s response exceeded %d bytes" % (NODE_ENDPOINT_PATH, NODE_BODY_LIMIT))
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return _node_error_snapshot(display_url, "%s returned non-JSON payload" % NODE_ENDPOINT_PATH)
    parsed = parse_inference_node_document(document, base_url=display_url)
    if parsed is None:
        if isinstance(document, Mapping):
            raw_schema = document.get("schema")
            raw_api = document.get("api_version")
            if isinstance(raw_schema, str) and raw_schema and raw_schema != "sonder.inference.node/1":
                if raw_schema.startswith("sonder.inference."):
                    return _node_absent_snapshot(display_url, "%s returned %s" % (NODE_ENDPOINT_PATH, raw_schema[:64]))
                return _node_error_snapshot(display_url, "incompatible node schema")
            if raw_api is not None and raw_api != 1:
                return _node_error_snapshot(display_url, "incompatible node api_version")
        return _node_absent_snapshot(display_url, "%s has no node metadata" % NODE_ENDPOINT_PATH)
    return parsed


def _gateway_get_transport(
    gateway: object | None,
) -> Callable[[str, dict[str, str], float], tuple[int, bytes]] | None:
    if gateway is None:
        return None
    seams = getattr(gateway, "_transport_seams", None)
    if isinstance(seams, tuple) and len(seams) >= 2:
        return seams[1] if callable(seams[1]) else None
    direct = getattr(gateway, "_get_transport", None)
    if callable(direct):
        return direct
    return None


def _resolve_node_settings(gateway: object | None, env: Mapping[str, str] | None) -> Any:
    from .sonder_inference_gateway import config_from_env

    if gateway is not None:
        settings_fn = getattr(gateway, "settings", None)
        if callable(settings_fn):
            return settings_fn()
    return config_from_env(env)


def _worker_node_settings(
    primary_settings: Any,
    spec: Any,
    *,
    env: Mapping[str, str] | None = None,
) -> Any:
    from .sonder_inference_gateway import (
        MIN_WORKER_TOKEN_LENGTH,
        SonderInferenceConfig,
    )

    source = os.environ if env is None else env
    token = str(source.get(spec.token_env, "") or "").strip()
    if len(token) < MIN_WORKER_TOKEN_LENGTH:
        raise Forbidden(
            "private sonder-inference worker %s: %s is unset or shorter than %d characters"
            % (spec.display_url, spec.token_env, MIN_WORKER_TOKEN_LENGTH)
        )
    return SonderInferenceConfig(
        base_url=spec.url,
        model=primary_settings.model,
        tier_models=dict(primary_settings.tier_models),
        api_key=token,
        allow_remote=True,
        timeout_seconds=primary_settings.timeout_seconds,
        stream_idle_seconds=primary_settings.stream_idle_seconds,
        stream_first_content_seconds=primary_settings.stream_first_content_seconds,
        health_ttl_seconds=primary_settings.health_ttl_seconds,
        base_url_source="private_worker",
        health_timeout_seconds=primary_settings.health_timeout_seconds,
        health_stale_seconds=primary_settings.health_stale_seconds,
        private_worker=True,
        ca_bundle=spec.ca_bundle,
        max_inflight=spec.max_inflight,
    )


def inspect_inference_node(
    *,
    gateway: object | None = None,
    env: Mapping[str, str] | None = None,
    settings: Any | None = None,
    get_transport: Callable[[str, dict[str, str], float], tuple[int, bytes]] | None = None,
    timeout: float = NODE_PROBE_TIMEOUT_SECONDS,
    ttl: float = DEFAULT_NODE_TTL_SECONDS,
    refresh: bool = False,
    cache: NodeSnapshotCache | None = None,
) -> InferenceNodeSnapshot:
    """Read and parse ``GET /v1/sonder/node`` for one Sonder Inference endpoint.

    Enforces loopback/remote-consent/HTTPS/pinned-CA policy via
    ``check_endpoint_policy`` + ``trust_scope``, bounds wall-clock time and
    payload size, caches per endpoint (skipped when an explicit test
    ``get_transport`` or gateway custom transport seam is injected unless a
    dedicated ``cache`` is supplied), and never raises.
    """
    try:
        resolved = settings if settings is not None else _resolve_node_settings(gateway, env)
    except Exception as exc:  # noqa: BLE001 - diagnostics never raise
        return _node_error_snapshot(
            "",
            str(exc) if isinstance(exc, SonderError) else type(exc).__name__,
        )
    transport = get_transport if get_transport is not None else _gateway_get_transport(gateway)
    effective_cache = cache
    if effective_cache is None and transport is None:
        effective_cache = _NODE_CACHE
    if effective_cache is None:
        return _fetch_node_for_settings(resolved, timeout=timeout, get_transport=transport)
    return effective_cache.get(
        str(resolved.base_url),
        lambda: _fetch_node_for_settings(resolved, timeout=timeout, get_transport=transport),
        ttl=ttl,
        refresh=refresh,
    )


def inspect_inference_nodes(
    *,
    gateway: object | None = None,
    env: Mapping[str, str] | None = None,
    get_transport: Callable[[str, dict[str, str], float], tuple[int, bytes]] | None = None,
    timeout: float = NODE_PROBE_TIMEOUT_SECONDS,
    ttl: float = DEFAULT_NODE_TTL_SECONDS,
    refresh: bool = False,
    cache: NodeSnapshotCache | None = None,
) -> dict[str, Any]:
    """Inspect primary and configured private-worker ``/v1/sonder/node`` GPU pools.

    Returns a content-free aggregate report with per-node snapshots, discrete vs
    integrated UMA VRAM totals, combined GPU VRAM pool, and hybrid GPU spread
    availability across all reachable nodes.
    """
    effective_env = env
    if effective_env is None and gateway is not None:
        gw_env = getattr(gateway, "_env", None)
        if isinstance(gw_env, Mapping):
            effective_env = gw_env
    try:
        primary_settings = _resolve_node_settings(gateway, effective_env)
    except Exception as exc:  # noqa: BLE001 - diagnostics never raise
        failed = _node_error_snapshot(
            "",
            str(exc) if isinstance(exc, SonderError) else type(exc).__name__,
        )
        return {
            "primary": failed.to_dict(),
            "workers": [],
            "total_nodes": 1,
            "ready_nodes": 0,
            "discrete_vram_mib": None,
            "integrated_uma_vram_mib": None,
            "combined_gpu_vram_mib": None,
            "combined_free_gpu_vram_mib": None,
            "hybrid_gpu_spread_nodes": [],
            "summary": "",
        }

    primary_snap = inspect_inference_node(
        gateway=gateway,
        env=effective_env,
        settings=primary_settings,
        get_transport=get_transport,
        timeout=timeout,
        ttl=ttl,
        refresh=refresh,
        cache=cache,
    )
    worker_snaps: list[InferenceNodeSnapshot] = []
    for spec in getattr(primary_settings, "workers", ()) or ():
        try:
            worker_cfg = _worker_node_settings(primary_settings, spec, env=effective_env)
        except Exception as exc:  # noqa: BLE001 - one bad worker must not break others
            worker_snaps.append(
                _node_error_snapshot(
                    str(getattr(spec, "display_url", "") or ""),
                    str(exc) if isinstance(exc, SonderError) else type(exc).__name__,
                )
            )
            continue
        worker_snaps.append(
            inspect_inference_node(
                gateway=gateway,
                env=effective_env,
                settings=worker_cfg,
                get_transport=get_transport,
                timeout=timeout,
                ttl=ttl,
                refresh=refresh,
                cache=cache,
            )
        )

    all_snaps = [primary_snap, *worker_snaps]
    ok_snaps = [snap for snap in all_snaps if snap.state == NODE_OK]
    discrete_total = 0
    uma_total = 0
    combined_total = 0
    combined_free = 0
    saw_pool = False
    hybrid_nodes: list[str] = []
    for snap in ok_snaps:
        pool = snap.gpu_pool
        if pool is not None:
            saw_pool = True
            discrete_total += pool.discrete_vram_mib
            uma_total += pool.integrated_uma_vram_mib
            combined_total += (
                snap.combined_gpu_vram_mib
                if snap.combined_gpu_vram_mib is not None
                else pool.combined_gpu_vram_mib
            )
            combined_free += pool.combined_free_gpu_vram_mib
            if snap.hybrid_gpu_spread and pool.hybrid_vulkan_pool_available and snap.base_url:
                hybrid_nodes.append(snap.base_url)
        elif snap.combined_gpu_vram_mib is not None:
            saw_pool = True
            combined_total += snap.combined_gpu_vram_mib

    return {
        "primary": primary_snap.to_dict(),
        "workers": [snap.to_dict() for snap in worker_snaps],
        "total_nodes": len(all_snaps),
        "ready_nodes": len(ok_snaps),
        "discrete_vram_mib": discrete_total if saw_pool else None,
        "integrated_uma_vram_mib": uma_total if saw_pool else None,
        "combined_gpu_vram_mib": combined_total if saw_pool else None,
        "combined_free_gpu_vram_mib": combined_free if saw_pool else None,
        "hybrid_gpu_spread_nodes": hybrid_nodes,
        "summary": format_inference_nodes_summary(primary_snap, worker_snaps),
    }


def format_inference_nodes_summary(
    primary: InferenceNodeSnapshot,
    workers: Sequence[InferenceNodeSnapshot] = (),
) -> str:
    """Compact content-free doctor detail string for primary + private worker GPU pools."""
    segments: list[str] = []
    primary_line = format_node_gpu_pool_summary(primary)
    if primary_line:
        segments.append(primary_line)
    for worker in workers:
        w_line = format_node_gpu_pool_summary(worker)
        if w_line:
            label = worker.base_url or "worker"
            segments.append("worker %s %s" % (label, w_line))
    return "; ".join(segments)


__all__ = [
    "DEFAULT_NODE_TTL_SECONDS",
    "ENV_EMBED_BASE_URL",
    "ENV_EMBED_ON_CPU",
    "ENV_KEEP_PRIMARY_RESIDENT",
    "ENV_OLLAMA_LLM_LIBRARY",
    "NODE_BODY_LIMIT",
    "NODE_ENDPOINT_PATH",
    "NODE_PROBE_TIMEOUT_SECONDS",
    "NodeSnapshotCache",
    "RESIDENT_KEEP_ALIVE",
    "cached_inference_node",
    "format_inference_nodes_summary",
    "gpu_sharing_findings",
    "gpu_sharing_report",
    "inspect_inference_node",
    "inspect_inference_nodes",
    "keep_alive_for",
    "keep_primary_resident",
    "local_ollama_pinned_to_cpu",
    "local_ollama_tier_models",
    "shared_node_cache",
]
