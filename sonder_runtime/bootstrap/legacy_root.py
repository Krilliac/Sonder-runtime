"""Single compatibility boundary for the historical :mod:`server` module."""
from __future__ import annotations

from types import ModuleType
import threading

_owned_application = None


def require_inference_application(application, *, expected_pool=None, allow_inactive=False):
    """Validate the trusted composition without invoking structural lookalikes."""
    from .application_graph import Application
    from ..adapters.inference.ollama_pool import OllamaWorkerPool
    from ..adapters.inference.static_membership import StaticMembershipSource, configured_worker_origins
    from ..application.inference_membership.controller import MembershipController
    from ..platform.config import SonderConfig, validate_membership_config

    if type(application) is not Application or type(application.config) is not SonderConfig:
        raise ValueError("invalid legacy membership binding: exact typed Application required")
    pool, controller = application.inference_pool, application.inference_membership
    if (type(pool) is not OllamaWorkerPool or type(controller) is not MembershipController
            or (expected_pool is not None and pool is not expected_pool)
            or controller._pool is not pool):
        raise ValueError("invalid legacy membership binding: exact pool/controller ownership required")
    configured = configured_worker_origins(application.config.ollama)
    validate_membership_config(application.config.membership, application.config.secrets,
                               allow_remote=application.config.ollama.allow_remote)
    origins = pool.configured_origins
    if (type(origins) is not tuple or any(type(origin) is not str for origin in origins)
            or origins != configured):
        raise ValueError("invalid legacy membership binding: configured origins differ")
    source = controller._source
    if application.config.membership.mode == "external":
        _require_external_source(application, source, controller, pool)
    else:
        if type(source) is not StaticMembershipSource or pool._external_source is not None or controller._high_water_store is not None:
            raise ValueError("invalid legacy membership binding: exact static configuration source required")
        origins = source.configured_origins
        if (type(origins) is not tuple or any(type(origin) is not str for origin in origins)
                or origins != configured):
            raise ValueError("invalid legacy membership binding: configured origins differ")
        source_origins = configured_worker_origins(source._configuration)
        if (source_origins != configured or source._clock is not controller._clock
                or source._configuration.worker_capability_ttl_seconds != application.config.ollama.worker_capability_ttl_seconds
                or source._configuration.worker_max_inflight != application.config.ollama.worker_max_inflight
                or any(type(value) is not str for value in (
                    source.cluster_id, source.issuer_id, controller._cluster, controller._issuer))
                or source.cluster_id != StaticMembershipSource.cluster_id
                or source.issuer_id != StaticMembershipSource.issuer_id
                or source.cluster_id != controller._cluster or source.issuer_id != controller._issuer):
            raise ValueError("invalid legacy membership binding: static source authority/configuration differs")
    authority = pool._membership_authority
    if ((allow_inactive is not True and (controller._closed is not False or pool._draining))
            or pool._membership_clock is None or pool._membership_clock is not controller._clock
            or type(authority) is not tuple or len(authority) != 2
            or any(type(value) is not str for value in (*authority, controller._cluster, controller._issuer))
            or authority[0] != controller._cluster or authority[1] != controller._issuer):
        raise ValueError("invalid legacy membership binding: configured membership must be active")
    return pool


def _require_external_source(application, source, controller, pool):
    from pathlib import Path
    from types import MappingProxyType
    from ..adapters.inference.external_membership import ExternalMembershipSource, _PinnedTransport
    from ..adapters.inference.membership_high_water import MembershipHighWaterStore
    from ..application.ports.inference_membership import MembershipSourceLimits
    from ..platform import paths
    from ..platform.config import MembershipEndpointPolicy
    config, secrets = application.config.membership, application.config.secrets
    water = controller._high_water_store
    limits = controller._source_limits
    if (type(source) is not ExternalMembershipSource or pool._external_source is not source
            or source._config is not config or source._secrets is not secrets
            or source._clock is not controller._clock or type(source._transport) is not _PinnedTransport
            or source._transport._config is not config or source._transport._secrets is not secrets
            or type(water) is not MembershipHighWaterStore or water._clock is not controller._clock
            or type(limits) is not MembershipSourceLimits
            or limits.max_advertisements != config.snapshot_max_advertisements
            or limits.max_bytes != config.snapshot_max_bytes
            or any(type(value) is not str for value in (
                source.cluster_id, source.issuer_id, controller._cluster, controller._issuer, water._cluster, water._issuer))
            or source.cluster_id != config.cluster_id or source.issuer_id != config.issuer_id
            or controller._cluster != config.cluster_id or controller._issuer != config.issuer_id
            or water._cluster != config.cluster_id or water._issuer != config.issuer_id
            or type(water._path) is not type(Path())
            or water._path != paths.default_home() / "inference-membership" / "high-water.json"):
        raise ValueError("invalid legacy membership binding: exact external authority required")
    policy = source._source_policy
    if (type(policy) is not MembershipEndpointPolicy
            or any(type(value) is not str for value in (policy.member_id, policy.origin, policy.tls_server_name))
            or type(policy.allowed_cidrs) is not tuple or any(type(value) is not str for value in policy.allowed_cidrs)
            or (policy.member_id, policy.origin, policy.tls_server_name, policy.allowed_cidrs) !=
               ("source", config.source_origin, config.source_tls_server_name, config.source_allowed_cidrs)
            or type(source._policies) is not MappingProxyType or type(source._origins) is not MappingProxyType
            or len(source._policies) != len(config.member_policies) or len(source._origins) != len(config.member_policies)
            or any(type(key) is not str for key in (*source._policies, *source._origins))
            or any(source._policies.get(value.member_id) is not value or source._origins.get(value.origin) is not value
                   for value in config.member_policies)
            or type(pool._configured_remote_origins) is not frozenset
            or any(type(value) is not str for value in pool._configured_remote_origins)
            or pool._configured_remote_origins != frozenset(source._origins)):
        raise ValueError("invalid legacy membership binding: external endpoint policy differs")


def require_mcp_inference_binding(application, pool, *, primary_origin):
    """Allow bare loopback legacy startup; every supplied typed graph is exact."""
    from .application_graph import Application
    from ..adapters.inference import ollama_endpoint
    from ..adapters.inference.ollama_pool import OllamaWorkerPool

    if type(pool) is not OllamaWorkerPool or type(primary_origin) is not str:
        raise ValueError("invalid legacy membership binding: exact pool and primary required")
    origins = pool.configured_origins
    if (type(origins) is not tuple or not origins or any(type(origin) is not str for origin in origins)
            or origins[0] != ollama_endpoint.normalize(primary_origin)):
        raise ValueError("invalid legacy membership binding: primary differs from configured pool")
    if ollama_endpoint.is_loopback(primary_origin) and not pool.has_configured_remote_workers:
        if application is None:
            return
        if (type(application) is Application and application.config is None
                and application.inference_pool is None and application.inference_membership is None):
            return
    require_inference_application(application, expected_pool=pool)
    if primary_origin != ollama_endpoint.normalize(application.config.ollama.url):
        raise ValueError("invalid legacy membership binding: primary differs from typed configuration")


def configure_application(application) -> None:
    """Bind an entrypoint-owned typed graph without replacing caller ownership."""
    global _owned_application
    pool = require_inference_application(application)
    legacy = runtime()
    if not legacy._APP_GRAPH_LOCK.acquire(timeout=5):
        raise RuntimeError("legacy application composition is busy")
    try:
        current = legacy._APP_GRAPH
        if current is not None and current is not application:
            if current is not _owned_application:
                raise RuntimeError("legacy runtime retains a caller-owned application")
            if type(current) is not type(application):
                raise ValueError("invalid legacy membership binding: exact outgoing Application required")
        previous_pool = legacy.OLLAMA_POOL
        if type(previous_pool) is not type(pool):
            raise ValueError("invalid legacy membership binding: exact outgoing pool required")
        if current is not None and current is not application:
            # A former owner may already be closed during re-composition, but
            # cleanup must still belong to exact trusted types and this pool.
            require_inference_application(current, expected_pool=previous_pool, allow_inactive=True)
            current.close_providers(timeout=5)
        from ..adapters.inference import ollama_endpoint

        if previous_pool is not pool:
            # Preloaded legacy modules must not retain a second admission
            # path. Existing work finishes against its original pool.
            previous_pool.drain(timeout_seconds=0)
        legacy.OLLAMA_POOL = pool
        legacy.BASE = ollama_endpoint.normalize(application.config.ollama.url)
        if current is application:
            return
        legacy._APP_GRAPH = application
        # ``server._application`` tracks only graphs it constructed itself.
        # A typed host handoff replaces that authority and must never be
        # retired by the legacy MCP adapter's local finalizer.
        legacy._APP_GRAPH_OWNED_BY_SERVER = False
        _owned_application = application
    finally:
        legacy._APP_GRAPH_LOCK.release()


def detach_owned_application(application) -> bool:
    """Remove an already-owned graph before its default close callback runs.

    This is an internal lifecycle handoff, never an external composition seam.
    It avoids asking a later reconfiguration to close a graph whose default
    owner has already detached and closed its non-repeatable providers.
    """
    global _owned_application
    from .application_graph import Application
    import sys

    if type(application) is not Application:
        return False
    legacy = sys.modules.get("server")
    if not isinstance(legacy, ModuleType):
        return False
    lock = getattr(legacy, "_APP_GRAPH_LOCK", None)
    if lock is None or not lock.acquire(timeout=5):
        raise RuntimeError("legacy application composition is busy")
    try:
        if getattr(legacy, "_APP_GRAPH", None) is application and _owned_application is application:
            legacy._APP_GRAPH = None
            _owned_application = None
            return True
        return False
    finally:
        lock.release()

def runtime() -> ModuleType:
    """Return the already-composed historical runtime module."""
    import server

    return server


class LazyRuntimeProxy:
    """Explicit interface dependency that loads ``server`` on first access."""

    def __init__(self) -> None:
        object.__setattr__(self, "_lock", threading.Lock())
        object.__setattr__(self, "_loaded", None)

    def _resolve(self) -> ModuleType:
        loaded = object.__getattribute__(self, "_loaded")
        if loaded is None:
            with object.__getattribute__(self, "_lock"):
                loaded = object.__getattribute__(self, "_loaded")
                if loaded is None:
                    loaded = runtime()
                    object.__setattr__(self, "_loaded", loaded)
        return loaded

    def __getattr__(self, name: str):
        return getattr(self._resolve(), name)

    def __setattr__(self, name: str, value) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
        else:
            setattr(self._resolve(), name, value)


def runtime_proxy() -> LazyRuntimeProxy:
    """Create an explicit lazy proxy for interface compatibility wiring."""
    return LazyRuntimeProxy()


def configure_capacity(
    *,
    autopilot_runs: int,
    fleet_workers: int,
    training_jobs: int,
) -> None:
    """Push capacity limits through the single allowed server import."""
    import server as legacy_server
    legacy_server.configure_capacity(
        autopilot_runs=autopilot_runs,
        fleet_workers=fleet_workers,
        training_jobs=training_jobs,
    )


__all__ = ["LazyRuntimeProxy", "configure_application", "configure_capacity",
            "detach_owned_application",
           "require_inference_application", "require_mcp_inference_binding", "runtime", "runtime_proxy"]
