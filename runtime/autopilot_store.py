"""Historical migration compatibility alias for the packaged autopilot store.

Production code imports ``sonder_runtime.adapters.persistence.autopilot_store``
directly. This root name remains only because the immutable autopilot baseline
migration imports it while replaying historical schema adoption.
"""
from __future__ import annotations

from sonder_runtime.adapters.persistence import autopilot_store as _store


def __getattr__(name):
    return getattr(_store, name)


def __dir__():
    return sorted(set(globals()) | set(dir(_store)))


_ensure_schema = _store._ensure_schema
