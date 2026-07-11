import pytest

import autopilot_store
import server
import trilobite_serve


@pytest.fixture(autouse=True)
def isolated_autopilot_db(monkeypatch, tmp_path):
    monkeypatch.setenv("TRILOBITE_AUTOPILOT_DB", str(tmp_path / "autopilot.db"))
    autopilot_store.reset_schema_cache_for_tests()
    yield
    autopilot_store.reset_schema_cache_for_tests()


def _plan(_run):
    return {
        "summary": "test plan",
        "success_criteria": ["real evidence is present"],
        "tasks": [
            {"title": "Inspect", "kind": "inspect", "instruction": "inspect"},
            {"title": "Validate", "kind": "validate", "instruction": "validate"},
        ],
    }


def _work(_run, task, _prior):
    tool = "workspace_run" if task["kind"] == "validate" else "file_read"
    return "done\n\n=== TOOL EVIDENCE ===\nstep 1 tool=%s reason=test\nPASS" % tool


def _review(_run, _issue):
    return {"decision": "complete", "reason": "verified", "tasks": []}


def test_waiting_start_runs_end_to_end_without_ollama(monkeypatch):
    monkeypatch.setattr(server, "_autopilot_plan_model", _plan)
    monkeypatch.setattr(server, "_autopilot_work_model", _work)
    monkeypatch.setattr(server, "_autopilot_review_model", _review)
    output = server.autopilot_start("finish the test objective", wait=True)
    assert "status/phase: completed / completed" in output
    assert "autopilot end report" in output
    stored = autopilot_store.get_run()
    assert stored["status"] == "completed"
    assert stored["adaptive"] is True
    assert stored["checkpoints"] == 1


def test_control_command_creates_background_goal(monkeypatch):
    launched = []
    monkeypatch.setattr(
        server, "_launch_autopilot",
        lambda run_id, max_cycles=12, plan_only=False: launched.append(
            (run_id, max_cycles, plan_only)
        ) or True,
    )
    output = server.control_command(
        "/autopilot plan --observe --no-web --static inspect this project",
        project="demo",
    )
    stored = autopilot_store.get_run()
    assert output.startswith("autopilot plan started")
    assert stored["policy"] == "observe"
    assert stored["allow_web"] is False
    assert stored["adaptive"] is False
    assert stored["project"] == "demo"
    assert launched == [(stored["id"], 12, True)]


def test_planner_reserves_room_for_adaptive_replans(monkeypatch):
    captured = {}

    def fake_json(run, role, prompt, validator):
        captured.update({"role": role, "prompt": prompt})
        payload = _plan(run)
        validator(payload)
        return payload

    monkeypatch.setattr(server, "_autopilot_json_model", fake_json)
    payload = server._autopilot_plan_model({
        "objective": "adapt safely", "project": "demo", "policy": "workspace",
        "allow_web": False, "adaptive": True, "max_tasks": 12,
        "max_replans": 2, "tier": "code",
    })

    assert payload["summary"] == "test plan"
    assert captured["role"] == "planner"
    assert "Initial task limit: 6" in captured["prompt"]
    assert "Replan budget: 2" in captured["prompt"]


def test_reviewer_receives_checkpoint_evidence_and_continue_decision(monkeypatch):
    captured = {}

    def fake_json(_run, role, prompt, validator):
        captured.update({"role": role, "prompt": prompt})
        payload = {
            "decision": "continue",
            "reason": "plan remains correct",
            "pending_assessment": [
                {"id": "task-02", "verdict": "keep", "reason": "still required"},
            ],
        }
        validator(payload)
        return payload

    monkeypatch.setattr(server, "_autopilot_json_model", fake_json)
    result = server._autopilot_review_model({
        "objective": "adapt", "failures": 0, "max_failures": 3,
        "max_tasks": 12, "checkpoints": 1, "replans": 0, "max_replans": 2,
        "plan": [{
            "id": "task-01", "kind": "inspect", "title": "Inspect",
            "instruction": "Inspect the real API",
            "status": "passed", "attempts": 1,
            "output": "found API\n=== TOOL EVIDENCE ===\nstep 1 tool=file_read reason=inspect",
        }, {
            "id": "task-02", "kind": "validate", "title": "Validate",
            "instruction": "Run focused tests",
            "status": "pending", "attempts": 0, "output": "",
        }],
    }, "adaptive checkpoint after task-01")

    assert result["decision"] == "continue"
    assert captured["role"] == "reviewer"
    assert '"evidence_actions": ["file_read: inspect"]' in captured["prompt"]
    assert '"instruction": "Run focused tests"' in captured["prompt"]
    assert '"pending_assessment"' in captured["prompt"]
    assert "complete|continue|retry|replan|pause" in captured["prompt"]


def test_reviewer_rejects_continue_when_pending_assessment_is_stale(monkeypatch):
    def fake_json(_run, _role, _prompt, validator):
        payload = {
            "decision": "continue",
            "reason": "nothing else needed",
            "pending_assessment": [
                {"id": "task-02", "verdict": "stale", "reason": "already exists"},
            ],
        }
        validator(payload)
        return payload

    monkeypatch.setattr(server, "_autopilot_json_model", fake_json)
    with pytest.raises(ValueError, match="continue is invalid"):
        server._autopilot_review_model({
            "objective": "adapt", "max_tasks": 6, "max_replans": 1,
            "plan": [
                {
                    "id": "task-01", "kind": "inspect", "title": "Inspect",
                    "instruction": "Inspect", "status": "passed", "output": "exists",
                },
                {
                    "id": "task-02", "kind": "research", "title": "Design missing feature",
                    "instruction": "Assume it is absent", "status": "pending", "output": "",
                },
            ],
        }, "adaptive checkpoint after task-01")


def test_reviewer_fills_omitted_pending_assessments_as_keep(monkeypatch):
    captured = {}

    def fake_json(_run, _role, _prompt, validator):
        payload = {
            "decision": "replan",
            "reason": "one premise is stale",
            "pending_assessment": [
                {"id": "task-02", "verdict": "stale", "reason": "already exists"},
            ],
            "tasks": [],
        }
        validator(payload)
        captured.update(payload)
        return payload

    monkeypatch.setattr(server, "_autopilot_json_model", fake_json)
    result = server._autopilot_review_model({
        "objective": "adapt", "max_tasks": 6, "max_replans": 1,
        "plan": [
            {
                "id": "task-01", "kind": "inspect", "title": "Inspect",
                "instruction": "Inspect", "status": "passed", "output": "exists",
            },
            {
                "id": "task-02", "kind": "research", "title": "Design",
                "instruction": "Assume absent", "status": "pending", "output": "",
            },
            {
                "id": "task-03", "kind": "validate", "title": "Validate",
                "instruction": "Run tests", "status": "pending", "output": "",
            },
        ],
    }, "adaptive checkpoint after task-01")

    assert result["decision"] == "replan"
    assert captured["pending_assessment"][-1] == {
        "id": "task-03",
        "verdict": "keep",
        "reason": "host default: reviewer did not mark this pending task stale",
    }


def test_autopilot_policy_blocks_control_plane_shell_and_bypass():
    run = {"policy": "workspace"}
    check = server._autopilot_tool_policy(run)
    assert "not approved" in check("workspace_run", {"program": "git"})
    assert "only accepts" in check("script_run", {"path": "build.ps1"})
    assert "bypass" in check("file_read", {"path": "x", "token": "secret"})
    assert check("workspace_run", {"program": "python"}) == ""
    assert "approximate_location_lookup" not in server._AUTOPILOT_WORKSPACE_TOOLS
    assert "file_delete" not in server._AUTOPILOT_WORKSPACE_TOOLS
    assert "master_orchestrate" not in server._AUTOPILOT_WORKSPACE_TOOLS


def test_agent_host_allowlist_rejects_model_tool_expansion(monkeypatch):
    responses = [
        '{"tool":"file_delete","args":{"path":"x"}}',
        '{"final":"stopped after host denial"}',
    ]
    dispatched = []
    monkeypatch.setattr(
        server, "_make_generate",
        lambda *args, **kwargs: lambda prompt, history=None: responses.pop(0),
    )
    monkeypatch.setattr(
        server, "_agent_dispatch_observed",
        lambda *args, **kwargs: dispatched.append(args) or "unexpected",
    )
    output = server._agent_impl(
        "inspect only", max_steps=2, include_evidence=True,
        tool_allowlist={"file_read"},
    )
    assert output.startswith("stopped after host denial")
    assert dispatched == []
    assert "outside this autonomous run's allowlist" in output


def test_http_autopilot_status_is_safe_but_lifecycle_changes_require_developer():
    assert trilobite_serve._dangerous_http_slash("/autopilot") is False
    assert trilobite_serve._dangerous_http_slash("/autopilot status auto-1") is False
    assert trilobite_serve._dangerous_http_slash("/autopilot run change files") is True
    assert trilobite_serve._dangerous_http_slash("/auto cancel auto-1") is True


def test_diagnostics_manifest_and_improvement_expose_autopilot(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setattr(server, "_get", lambda path: {"models": []})
    assert "autopilot:" in server.diagnostics()
    assert "execution routing: host-gated" in server.diagnostics()
    assert "autopilot_start" in server.tool_manifest()
    assert "/autopilot" in server.command_registry_list("agents")
    report = server.improvement_report_data()
    assert "autopilot" in report
