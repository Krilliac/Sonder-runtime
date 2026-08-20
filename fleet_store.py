"""Historical migration compatibility alias for the packaged fleet store.

Production code imports ``sonder_runtime.adapters.persistence.fleet_store``
directly. This root name remains only for the immutable fleet baseline
migration.
"""
from __future__ import annotations

from sonder_runtime.adapters.persistence import fleet_store as _store


def __getattr__(name):
    return getattr(_store, name)


def __dir__():
    return sorted(set(globals()) | set(dir(_store)))


_ensure_schema = _store._ensure_schema
