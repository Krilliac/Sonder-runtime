"""Single compatibility boundary for the historical :mod:`server` module."""
from __future__ import annotations

from types import ModuleType
import threading

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


__all__ = ["LazyRuntimeProxy", "configure_capacity", "runtime", "runtime_proxy"]
