"""The nightly isolation choice must not leak into ordinary selfmod calls."""

from __future__ import annotations

import importlib
import os
import sqlite3
import sys
from contextlib import nullcontext

import pytest


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


@pytest.mark.skipif(os.name == "nt", reason="unsupported-host isolation contract")
@pytest.mark.parametrize("low_integrity,environment", [
    (True, ""),
    (None, "1"),
])
@pytest.mark.parametrize("expect_failure", [False, True])
def test_unavailable_low_integrity_never_executes_candidate_or_passes_negative_gate(
    tmp_path, monkeypatch, low_integrity, environment, expect_failure,
):
    import selfmod

    calls = []
    records = []

    class Connection:
        def execute(self, _sql, parameters):
            records.append(parameters)

    monkeypatch.setenv("SELFMOD_LOW_INTEGRITY", environment)
    monkeypatch.setattr(
        selfmod, "_run",
        lambda *args: calls.append(args) or (7, "untrusted candidate ran", 1),
    )
    monkeypatch.setattr(selfmod, "_tx", lambda: nullcontext(Connection()))
    monkeypatch.setattr(selfmod, "_event", lambda *_args: None)

    result = selfmod._record_command(
        {"id": "untrusted-run"}, "reproducer_before" if expect_failure else "held_out",
        ["python", "-c", "print('candidate')"], tmp_path, 5,
        expect_failure=expect_failure, low_integrity=low_integrity,
    )

    assert calls == []
    assert result["exit_code"] == 125
    assert result["passed"] is False
    assert "isolation unavailable" in result["output"]
    assert records and records[0][3] == 125 and records[0][6] == 0


@pytest.mark.skipif(os.name == "nt", reason="unsupported-host isolation contract")
def test_nightly_gate_never_executes_an_unisolated_truth_tamper(
    tmp_path, monkeypatch,
):
    import selfmod
    from scripts import nightly_selfmod

    truth = tmp_path / "evaluator.txt"
    receipt = tmp_path / "promotion-receipt.txt"
    rollback = tmp_path / "rollback-baseline.txt"
    for path in (truth, receipt, rollback):
        path.write_text("trusted\n", encoding="utf-8")

    class Connection:
        def execute(self, *_args):
            pass

    monkeypatch.setattr(selfmod, "get_run", lambda _id: {
        "id": "untrusted-run", "phase": "testing", "budgets": {"max_test_seconds": 5},
    })
    monkeypatch.setattr(selfmod, "candidate_path", lambda _id: tmp_path)
    monkeypatch.setattr(selfmod, "_tx", lambda: nullcontext(Connection()))
    monkeypatch.setattr(selfmod, "_event", lambda *_args: None)
    command = [
        sys.executable, "-c",
        "from pathlib import Path; "
        "[Path(path).write_text('tampered\\n') for path in %r]"
        % [str(path) for path in (truth, receipt, rollback)],
    ]

    result = nightly_selfmod._record_candidate_test(
        "untrusted-run", "held_out", command, timeout=5,
        protected_paths=(truth, receipt, rollback),
    )

    assert result["exit_code"] == 125
    assert result["passed"] is False
    assert "unsupported platform" in result["output"]
    assert all(path.read_text(encoding="utf-8") == "trusted\n"
               for path in (truth, receipt, rollback))


@pytest.mark.parametrize("requested", [None, False])
def test_auto_low_risk_candidate_requires_supervisor_even_without_nightly(
    tmp_path, monkeypatch, requested,
):
    import selfmod
    from scripts import selfmod_low_integrity

    dispatched = []
    records = []

    class Connection:
        def execute(self, _sql, parameters):
            records.append(parameters)

    monkeypatch.delenv("SELFMOD_LOW_INTEGRITY", raising=False)
    monkeypatch.setattr(selfmod, "_run", lambda *_args: dispatched.append("medium") or (
        0, 'SELFMOD ISOLATION: {"integrity": "low"}', 1,
    ))
    monkeypatch.setattr(selfmod_low_integrity, "run_isolated", lambda *_args, **_kwargs: (
        dispatched.append("supervisor") or
        {"exit_code": 0, "output": "done", "passed": True,
         "job": {"integrity": "low"}}
    ))
    monkeypatch.setattr(selfmod, "_tx", lambda: nullcontext(Connection()))
    monkeypatch.setattr(selfmod, "_event", lambda *_args: None)

    run = {"id": "auto-untrusted", "mode": "auto-low-risk", "risk": "low",
           "approval_required": False}
    if requested is False:
        with pytest.raises(PermissionError, match="low-integrity"):
            selfmod._record_command(run, "regression", [sys.executable, "-c", "pass"],
                                    tmp_path, 5, low_integrity=requested)
        assert dispatched == [] and records == []
    else:
        result = selfmod._record_command(
            run, "regression", [sys.executable, "-c", "pass"], tmp_path, 5,
            low_integrity=requested,
        )
        assert dispatched == ["supervisor"]
        assert result["passed"] is True
        assert records and records[0][-1] == "low"


@pytest.mark.parametrize("reported", [None, {"integrity": "medium"}, {"integrity": "LOW"}])
def test_supervisor_missing_or_conflicting_integrity_cannot_pass(
    tmp_path, monkeypatch, reported,
):
    import selfmod
    from scripts import selfmod_low_integrity

    records = []

    class Connection:
        def execute(self, _sql, parameters):
            records.append(parameters)

    monkeypatch.setattr(selfmod_low_integrity, "run_isolated", lambda *_args, **_kwargs: {
        "exit_code": 0, "output": 'SELFMOD ISOLATION: {"integrity": "low"}',
        "job": reported,
    })
    monkeypatch.setattr(selfmod, "_tx", lambda: nullcontext(Connection()))
    monkeypatch.setattr(selfmod, "_event", lambda *_args: None)

    outcome = selfmod._record_command(
        {"id": "untrusted-run", "mode": "auto-low-risk", "risk": "low",
         "approval_required": False}, "held_out",
        [sys.executable, "-c", "pass"], tmp_path, 5,
    )
    assert outcome["passed"] is False and outcome["exit_code"] == 125
    assert "isolation unavailable" in outcome["output"]
    assert records and records[0][6] == 0 and records[0][-1] == "unverified"


def test_evaluator_integrity_failure_is_not_a_successful_negative_gate(
    tmp_path, monkeypatch,
):
    import selfmod
    from scripts import selfmod_low_integrity

    records = []

    class Connection:
        def execute(self, _sql, parameters):
            records.append(parameters)

    monkeypatch.setattr(selfmod_low_integrity, "run_isolated", lambda *_args, **_kwargs: {
        "exit_code": 2, "output": "protected truth changed", "passed": False,
        "integrity_failed": True, "job": {"integrity": "low"},
    })
    monkeypatch.setattr(selfmod, "_tx", lambda: nullcontext(Connection()))
    monkeypatch.setattr(selfmod, "_event", lambda *_args: None)

    result = selfmod._record_command(
        {"id": "auto-untrusted", "mode": "auto-low-risk", "risk": "low",
         "approval_required": False}, "reproducer_after",
        [sys.executable, "-c", "pass"], tmp_path, 5, expect_failure=True,
    )
    assert result["exit_code"] == 125 and result["passed"] is False
    assert records and records[0][-1] == "unverified"


def test_candidate_cannot_propose_a_change_to_its_isolation_supervisor():
    import selfmod

    assert selfmod.is_protected_path("scripts/selfmod_low_integrity.py")


def test_preexisting_selfmod_database_defaults_to_unverified(tmp_path, monkeypatch):
    import selfmod

    database = tmp_path / "legacy-selfmod.db"
    monkeypatch.setenv("SONDER_SELFMOD_DB", str(database))
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE selfmod_runs (id TEXT PRIMARY KEY, phase TEXT, updated_ts REAL)")
        conn.execute("""CREATE TABLE selfmod_tests (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, kind TEXT,
            command_json TEXT, exit_code INTEGER, duration_ms INTEGER,
            output TEXT, passed INTEGER, created_ts REAL
        )""")
        conn.execute("INSERT INTO selfmod_tests(run_id,kind,command_json,exit_code,duration_ms,output,passed,created_ts) "
                     "VALUES('legacy','held_out','[]',0,1,'SELFMOD ISOLATION: low',1,0)")
        conn.execute("INSERT INTO selfmod_runs(id,phase,updated_ts) VALUES('legacy','reviewing',0)")

    with selfmod._connect() as conn:
        assert conn.execute("SELECT isolation FROM selfmod_tests").fetchone()[0] == "unverified"
        assert conn.execute("SELECT auto_evaluation_eligible FROM selfmod_runs").fetchone()[0] == 0


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
