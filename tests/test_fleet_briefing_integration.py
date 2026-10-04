"""Durable host regression; requires the standard isolated pytest state."""
import master_orchestrator as mo
import server
import pytest
from pathlib import Path


@pytest.fixture(autouse=True)
def isolated_master(monkeypatch):
    mo.reset_for_tests()
    monkeypatch.setattr(mo, "start_delegated", mo.run_delegated)
    monkeypatch.setattr(mo, "capacity", lambda *a, **k: {"worker_slots": 1})
    monkeypatch.setattr(mo, "parallel_worker_slots", lambda *a, **k: 1)
    monkeypatch.setattr(server, "_orchestrator_worker", lambda *a, **k: lambda prompt: "audited")
    yield
    mo.reset_for_tests()


def test_clean_task_and_angle_digests_reach_durable_rows():
    server.master_orchestrate("fleet 0 compare ideas")
    rows = mo.snapshot(limit=100)["agents"]
    master, = [row for row in rows if row["role"] == "master"]
    digest = mo.fleet_provenance.task_digest("compare ideas")
    assert master["task"] == "compare ideas"
    assert master["master_task_digest"] == master["delegated_task_digest"] == digest
    children = [row for row in rows if row["role"] == "agent"]
    assert len(children) == 3
    for child in children:
        assert child["master_task_digest"] == digest
        assert child["delegated_task_digest"] == mo.fleet_provenance.task_digest(child["task"])
        assert "fleet 0" not in child["task"]
        assert "Angle " in child["task"]


def _build_receipt(root, *, files=(), checks_run=True, checks_passed=True):
    return mo.RepositoryWorkerResult(
        "completed\n=== TOOL EVIDENCE ===\nfile_write; file_check", str(root),
        ("directory_tree", "file_write", "file_check"), files, checks_run, checks_passed,
    )


def test_build_fleet_receipts_cover_each_worker_and_reach_audit(monkeypatch, tmp_path):
    monkeypatch.setattr(mo.fleet_creations, "default_home", lambda: tmp_path)
    seen, audits = [], []

    def worker(prompt, project):
        folder = Path(project)
        seen.append(folder)
        assert "make me something cool" in prompt and "fleet 0" not in prompt
        (folder / "app.py").write_text("print('hello')\n", encoding="utf-8")
        return _build_receipt(folder)

    monkeypatch.setattr(server, "_orchestrator_agent_worker", lambda *a, **k: worker)
    monkeypatch.setattr(server, "_orchestrator_worker", lambda *a, **k: lambda prompt: audits.append(prompt) or "audit")
    response = server.master_orchestrate("fleet 0 make me something cool")
    master = next(row for row in mo.snapshot(limit=100)["agents"] if row["role"] == "master")
    root = tmp_path / "creations" / master["id"]
    assert str(root) in response
    assert {folder.name for folder in seen} == {"worker-01", "worker-02", "worker-03"}
    for folder in seen:
        assert folder.parent == root
        assert "folder=" + str(folder) in master["output"]
        assert not mo.fleet_creations.is_provisioned_worker(folder)
    assert master["master_task_digest"] == mo.fleet_provenance.task_digest("make me something cool")
    assert "best_candidate=" in master["output"] and "checks passed" in master["output"]
    assert "app.py" in master["output"]
    assert "HOST CREATION ROOT" in audits[0]
    assert "produce a concrete proposal/plan" not in audits[0]


def test_build_failure_still_lists_every_folder_and_partial_artifact(monkeypatch, tmp_path):
    monkeypatch.setattr(mo.fleet_creations, "default_home", lambda: tmp_path)

    def failed_worker(prompt, project):
        (Path(project) / "partial.py").write_text("partial", encoding="utf-8")
        raise ValueError("write was interrupted before validation")

    result = mo.run_delegated("create an app", failed_worker, lambda p: pytest.fail("no auditable outputs"),
                              agents=2, build_workspace=True)
    assert result["output_workspace"] in result["output"]
    assert "worker-01" in result["output"] and "worker-02" in result["output"]
    assert "partial.py" in result["output"]
    assert "checks=unknown" in result["output"] and "best_candidate=none" in result["output"]


def test_build_workspace_failure_closes_master_without_dispatch(monkeypatch):
    def refuse(*args, **kwargs):
        raise PermissionError("workspace unavailable")

    monkeypatch.setattr(mo.fleet_creations, "create_workspace", refuse)
    with pytest.raises(PermissionError):
        mo.run_delegated("create a game", lambda p: pytest.fail("worker started"), lambda p: "audit",
                         build_workspace=True)
    rows = mo.snapshot(limit=100)["agents"]
    assert len(rows) == 1 and rows[0]["status"] == "failed"
    assert "workspace provisioning failed" in rows[0]["error"]


def test_advise_fleet_does_not_provision_workspace(monkeypatch):
    monkeypatch.setattr(mo.fleet_creations, "create_workspace", lambda *a, **k: pytest.fail("advice created files"))
    result = server.master_orchestrate("fleet 2 compare approaches")
    assert "proposals" in result and "/autopilot" in result


def test_supplied_project_preserves_readonly_repository_lane(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(mo.fleet_creations, "create_workspace", lambda *a, **k: pytest.fail("existing project replaced"))

    def factory(tier, project, **kwargs):
        calls.append((project, kwargs))
        return lambda p, root: _build_receipt(root, checks_run=False, checks_passed=None)

    monkeypatch.setattr(server, "_orchestrator_agent_worker", factory)
    server.master_orchestrate("build an app", mode="delegate", agents=1, project=str(tmp_path))
    assert calls == [(str(tmp_path.resolve()), {})]
