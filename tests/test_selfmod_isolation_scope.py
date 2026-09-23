"""The nightly isolation choice must not leak into ordinary selfmod calls."""

from __future__ import annotations

import importlib
import os


def test_importing_nightly_does_not_mutate_low_integrity_environment(monkeypatch):
    monkeypatch.delenv("SELFMOD_LOW_INTEGRITY", raising=False)
    import scripts.nightly_selfmod as nightly_selfmod

    importlib.reload(nightly_selfmod)
    assert "SELFMOD_LOW_INTEGRITY" not in os.environ


def test_ordinary_record_test_keeps_legacy_default(monkeypatch, tmp_path):
    import selfmod

    captured = {}
    monkeypatch.delenv("SELFMOD_LOW_INTEGRITY", raising=False)
    monkeypatch.setattr(
        selfmod, "get_run",
        lambda _run_id: {"id": "run-1", "phase": "testing", "budgets": {"max_test_seconds": 10}},
    )
    monkeypatch.setattr(selfmod, "candidate_path", lambda _run_id: tmp_path)

    def record(*args, **kwargs):
        captured.update(kwargs)
        return {"passed": True}

    monkeypatch.setattr(selfmod, "_record_command", record)
    selfmod.record_test("run-1", "regression", ["python", "-V"])
    assert captured["low_integrity"] is None


def test_nightly_candidate_helper_selects_low_integrity_explicitly(monkeypatch):
    from scripts import nightly_selfmod

    captured = {}

    def record(*args, **kwargs):
        captured.update(kwargs)
        return {"passed": True}

    monkeypatch.setattr(nightly_selfmod.selfmod, "record_test", record)
    nightly_selfmod._record_candidate_test(
        "run-1", "regression", ["python", "-V"], timeout=10,
    )
    assert captured["low_integrity"] is True
