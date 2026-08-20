"""Packaged ownership tests for speculative-execution configuration."""

from pathlib import Path

import sonder_speculation
from sonder_runtime.platform import paths
from sonder_runtime.platform import speculation as packaged


def test_root_configuration_helpers_preserve_packaged_identity():
    assert sonder_speculation.min_saving_seconds is packaged.min_saving_seconds
    assert sonder_speculation.speculation_slots is packaged.speculation_slots
    assert sonder_speculation.predictor_path is packaged.predictor_path


def test_packaged_configuration_keeps_environment_boundaries(monkeypatch):
    monkeypatch.setenv("SONDER_SPECULATION_MIN_SAVING_MS", "-50")
    assert packaged.min_saving_seconds() == 0.0
    monkeypatch.setenv("SONDER_SPECULATION_MIN_SAVING_MS", "not-a-number")
    assert packaged.min_saving_seconds() == 0.04

    monkeypatch.setenv("SONDER_SPECULATION_SLOTS", "99")
    assert packaged.speculation_slots() == packaged.MAX_SLOTS
    monkeypatch.setenv("SONDER_SPECULATION_SLOTS", "0")
    assert packaged.speculation_slots() == 1


def test_predictor_path_uses_packaged_state_boundary(monkeypatch, tmp_path):
    override = tmp_path / "predictor.json"
    monkeypatch.setenv("SONDER_BRANCH_PREDICTOR", str(override))
    assert packaged.predictor_path() == override

    monkeypatch.delenv("SONDER_BRANCH_PREDICTOR")
    expected = Path(paths.state_path("branch_predictor.json"))
    assert packaged.predictor_path() == expected
