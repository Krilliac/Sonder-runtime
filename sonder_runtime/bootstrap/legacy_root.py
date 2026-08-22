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


__all__ = ["LazyRuntimeProxy", "runtime", "runtime_proxy"]
