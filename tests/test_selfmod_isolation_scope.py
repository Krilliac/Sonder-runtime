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


def test_deploy_refuses_bytes_that_differ_from_tested_digests(monkeypatch, tmp_path):
    import contextlib
    import hashlib

    import selfmod

    root, workspace = tmp_path / "root", tmp_path / "workspace"
    root.mkdir()
    workspace.mkdir()
    (root / "reflection.py").write_text("old\n", encoding="utf-8")
    (workspace / "reflection.py").write_text("tested\n", encoding="utf-8")
    tested = {"reflection.py": hashlib.sha256(b"tested\n").hexdigest()}
    # The candidate rewrote its file after the gates ran.
    (workspace / "reflection.py").write_text("swapped\n", encoding="utf-8")
    copies, phases = [], []
    run = {"phase": "approved", "repository_root": str(root), "files": ["reflection.py"]}
    monkeypatch.setattr(selfmod, "get_run", lambda _run_id: dict(run))
    monkeypatch.setattr(selfmod, "deployment_lock", lambda _run_id: contextlib.nullcontext("owner"))
    monkeypatch.setattr(selfmod, "verify_backup", lambda _run_id: None)
    monkeypatch.setattr(selfmod, "_current_source_matches", lambda _run: (True, ""))
    monkeypatch.setattr(selfmod, "inspect_diff", lambda _run_id: {"changed_files": ["reflection.py"]})
    monkeypatch.setattr(selfmod, "_renew_deployment_lock", lambda _owner: None)
    monkeypatch.setattr(selfmod, "candidate_path", lambda _run_id: workspace)
    monkeypatch.setattr(selfmod, "_atomic_copy", lambda *a, **k: copies.append(a))
    monkeypatch.setattr(selfmod, "_phase", lambda *a, **k: phases.append(a))
    monkeypatch.setattr(selfmod, "restore", lambda *a, **k: None)
    monkeypatch.setattr(selfmod, "tested_digests", lambda _run_id: {"files": tested, "diff_sha256": "d"})

    import pytest
    with pytest.raises(RuntimeError, match="differ from tested bytes"):
        selfmod.deploy("run-1", expected_digests=tested)
    assert copies == []
    assert (root / "reflection.py").read_text(encoding="utf-8") == "old\n"
