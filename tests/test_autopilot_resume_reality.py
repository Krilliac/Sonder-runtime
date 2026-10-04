"""Resumed Autopilot plans must regain authority from new host evidence."""
import os
import subprocess

import pytest

import autopilot_controller as controller
from sonder_runtime.adapters.persistence import autopilot_store as store


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("SONDER_AUTOPILOT_DB", str(tmp_path / "runs.db"))
    store.reset_schema_cache_for_tests()
    root = tmp_path / "project"
    root.mkdir()
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True)
    (root / "main.py").write_text("old\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "main.py"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", "base"], check=True, capture_output=True)
    yield root
    store.reset_schema_cache_for_tests()


def plan(_run):
    return {"summary": "original", "success_criteria": ["verified"], "tasks": [
        {"title": "Change", "kind": "implement", "instruction": "Change source"},
        {"title": "Validate", "kind": "validate", "instruction": "Run checks"},
    ]}


def receipt(_run, task, _prior):
    kind = task["kind"]
    return controller.HostTaskResult("done", tools=("file_read" if kind == "inspect" else "workspace_run",),
                                     mutation_observed=kind == "implement",
                                     validation_attempted=kind == "validate", validation_passed=kind == "validate")


def start(root):
    run = store.create_run("change source", project=str(root), adaptive=False)
    result = controller.execute_run(run["id"], "one", owner_pid=os.getpid(),
                                    plan_fn=plan, work_fn=receipt,
                                    review_fn=lambda *_: {"decision": "complete"}, plan_only=True)
    assert result["status"] == "paused"
    return run["id"]


def test_identical_resume_uses_no_recovery_callback(repo):
    run_id = start(repo)
    calls = []
    def work(run, task, prior):
        calls.append((task["kind"], prior))
        return receipt(run, task, prior)
    result = controller.execute_run(run_id, "two", owner_pid=os.getpid(), plan_fn=plan,
                                    work_fn=work, review_fn=lambda *_: {"decision": "complete"})
    assert result["status"] == "completed"
    assert [kind for kind, _ in calls] == ["implement", "validate"]
    assert all("resume_reality" not in prior for _, prior in calls)


def test_dirty_resume_requires_inspection_and_fresh_replan(repo):
    run_id = start(repo)
    (repo / "main.py").write_text("changed externally\n", encoding="utf-8")
    calls = []
    def work(run, task, prior):
        calls.append((task["kind"], prior))
        return receipt(run, task, prior)
    def review(_run, issue):
        if "resume" in issue.lower():
            return {"decision": "replan", "tasks": [
                {"title": "New change", "kind": "implement", "instruction": "Use fresh inspection"},
            ]}
        return {"decision": "complete"}
    result = controller.execute_run(run_id, "two", owner_pid=os.getpid(), plan_fn=plan,
                                    work_fn=work, review_fn=review)
    assert calls[0][0] == "inspect"
    assert "resume_reality" in calls[0][1]
    assert sum("resume_reality" in prior for _, prior in calls) == 1
    assert result["status"] == "completed"
    assert result["replans"] == 1


def test_prose_cannot_clear_barrier_even_on_second_resume(repo):
    run_id = start(repo)
    (repo / "main.py").write_text("external\n", encoding="utf-8")
    kinds = []
    def work(_run, task, _prior):
        kinds.append(task["kind"])
        return "I inspected and replanned"
    for owner in ("two", "three"):
        result = controller.execute_run(run_id, owner, owner_pid=os.getpid(), plan_fn=plan,
                                        work_fn=work, review_fn=lambda *_: {"decision": "continue"})
        assert result["status"] == "paused"
    assert kinds == ["inspect", "inspect"]


def test_inspection_pass_blocks_legacy_effects_and_clears_old_certificates(repo):
    import permission_modes as pm

    run_id = start(repo)
    store.claim_run(run_id, "seed", owner_pid=os.getpid())
    old = store.get_run(run_id)
    old["plan"][-1].update(status="passed", host_receipt={
        "tools": ["run_tests"], "validation_attempted": True, "validation_passed": True,
        "delegated_verification": {"certificate_id": "old", "generation": 1},
    })
    store.save_progress(run_id, "seed", plan=old["plan"])
    store.finish_run(run_id, "seed", "paused")
    (repo / "source.txt").write_text("external", encoding="utf-8")
    def work(run, task, prior):
        assert task["kind"] == "inspect"
        assert run["policy"] == "observe"
        decision = pm.decide("file_write", mode=pm.AUTO, interactive=False, record=False,
                             rule_lookup=lambda _: "allow")
        assert decision.action == pm.DENY and decision.source == "fence"
        return "no host receipt"
    result = controller.execute_run(run_id, "resume", owner_pid=os.getpid(), plan_fn=plan,
                                    work_fn=work, review_fn=lambda *_: {"decision": "complete"})
    assert result["status"] == "paused"
    assert not controller._completion_gate(result)[0]
    assert result["plan"][-1]["host_receipt"] == {}


def test_pending_barrier_blocks_store_completion(repo):
    run_id = start(repo)
    run = store.claim_run(run_id, "seed", owner_pid=os.getpid())
    for task in run["plan"]:
        task.update(status="passed", host_receipt=receipt(run, task, "").receipt())
    store.save_progress(run_id, "seed", plan=run["plan"])
    assert store.save_workspace_reality(run_id, "seed", {"pending": {"requires_replan": True}})
    assert store.finish_run(run_id, "seed", "completed") is None
