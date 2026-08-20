"""Immutable migration compatibility alias for the packaged queue ledger."""
from __future__ import annotations

from sonder_runtime.adapters.persistence import queued_actions as _store


def __getattr__(name):
    return getattr(_store, name)


def __dir__():
    return sorted(set(globals()) | set(dir(_store)))
