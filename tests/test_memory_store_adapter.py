"""Compatibility checks for the migrated SQLite memory adapter."""
from __future__ import annotations

import importlib

import memory_store
from sonder_runtime.adapters import memory_store as adapter_memory_store


def test_root_module_is_true_compatibility_alias(monkeypatch):
    assert memory_store is adapter_memory_store

    sentinel = object()
    monkeypatch.setattr(memory_store, "_compatibility_probe", sentinel, raising=False)
    assert adapter_memory_store._compatibility_probe is sentinel


def test_reload_preserves_module_and_process_shared_claim_state():
    session_lock = memory_store._ABANDONED_SESSION_CLAIMS_LOCK
    distillation_lock = memory_store._ABANDONED_DISTILLATION_CLAIMS_LOCK

    reloaded = importlib.reload(memory_store)

    assert reloaded is memory_store is adapter_memory_store
    assert reloaded._ABANDONED_SESSION_CLAIMS_LOCK is session_lock
    assert reloaded._ABANDONED_DISTILLATION_CLAIMS_LOCK is distillation_lock


def test_adapter_uses_domain_reward_rules():
    assert memory_store._good_outcome_signals() == (
        "accepted",
        "copied",
        "edited",
        "tests_passed",
        "used",
    )
