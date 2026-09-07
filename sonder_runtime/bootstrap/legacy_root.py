"""Single compatibility boundary for the historical :mod:`server` module."""
from __future__ import annotations

from types import ModuleType
import threading

_owned_application = None


def require_inference_application(application, *, expected_pool=None, allow_inactive=False):
    """Validate the trusted composition without invoking structural lookalikes."""
    from .application_graph import Application
    from ..adapters.inference.ollama_pool import OllamaWorkerPool
    from ..application.inference_membership.controller import MembershipController
    from ..platform.config import SonderConfig

    if type(application) is not Application or type(application.config) is not SonderConfig:
        raise ValueError("invalid legacy membership binding: exact typed Application required")
    pool, controller = application.inference_pool, application.inference_membership
    if (type(pool) is not OllamaWorkerPool or type(controller) is not MembershipController
            or (expected_pool is not None and pool is not expected_pool)
            or controller._pool is not pool):
        raise ValueError("invalid legacy membership binding: exact pool/controller ownership required")
    authority = pool._membership_authority
    if ((allow_inactive is not True and (controller._closed is not False or pool._draining))
            or pool._membership_clock is None or pool._membership_clock is not controller._clock
            or type(authority) is not tuple or len(authority) != 2
            or any(type(value) is not str for value in (*authority, controller._cluster, controller._issuer))
            or authority[0] != controller._cluster or authority[1] != controller._issuer):
        raise ValueError("invalid legacy membership binding: configured membership must be active")
    return pool


def require_mcp_inference_binding(application, pool, *, primary_origin):
    """Allow bare loopback legacy startup; every supplied typed graph is exact."""
    from .application_graph import Application
    from ..adapters.inference import ollama_endpoint
    from ..adapters.inference.ollama_pool import OllamaWorkerPool

    if type(pool) is not OllamaWorkerPool or type(primary_origin) is not str:
        raise ValueError("invalid legacy membership binding: exact pool and primary required")
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
        _owned_application = application
    finally:
        legacy._APP_GRAPH_LOCK.release()

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
           "require_inference_application", "require_mcp_inference_binding", "runtime", "runtime_proxy"]
