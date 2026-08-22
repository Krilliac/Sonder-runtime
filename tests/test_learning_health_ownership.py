"""Ownership and compatibility tests for the learning-health adapter."""
from __future__ import annotations

import importlib
from pathlib import Path

import learning_health
from sonder_runtime.adapters import learning_health as packaged


def test_root_learning_health_is_an_identity_compatibility_alias():
    assert learning_health is packaged
    assert Path(packaged.__file__).as_posix().endswith(
        "sonder_runtime/adapters/learning_health.py"
    )


def test_root_alias_preserves_private_provenance_and_gate_seams(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(learning_health, "_compatibility_probe", sentinel, raising=False)
    assert packaged._compatibility_probe is sentinel
    assert learning_health._REVIEWED_SOURCES == packaged._REVIEWED_SOURCES
    assert learning_health._AUTOGRADED_SOURCES == packaged._AUTOGRADED_SOURCES
    assert learning_health.gating_positive_percent is packaged.gating_positive_percent


def test_reload_of_legacy_name_resolves_to_packaged_implementation():
    reloaded = importlib.reload(learning_health)
    assert reloaded is packaged
    assert reloaded.build_report is packaged.build_report
    assert reloaded.format_report is packaged.format_report
