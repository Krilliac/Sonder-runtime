import json

import pytest

import autopilot_controller
import sonder_runtime.adapters.persistence.autopilot_store as autopilot_store
import server
import sonder_runtime.interfaces.http.serve as sonder_serve


@pytest.fixture(autouse=True)
def isolated_autopilot_db(monkeypatch, tmp_path):
    import permission_modes
    from sonder_runtime.platform import paths

    previous_home = paths._configured_home()
    paths.configure_home(tmp_path)
    (tmp_path / "permission_mode.json").write_text('{"mode": "auto"}', encoding="utf-8")
    monkeypatch.setattr(permission_modes, "_LOADED", False)
    monkeypatch.setattr(permission_modes, "_STATE", dict(permission_modes._STATE, mode="manual", elevated=False))
    monkeypatch.setattr(permission_modes, "_rule_lookup", lambda _name: None)
    monkeypatch.setenv("SONDER_AUTOPILOT_DB", str(tmp_path / "autopilot.db"))
    autopilot_store.reset_schema_cache_for_tests()
    yield
    autopilot_store.reset_schema_cache_for_tests()
    if previous_home is None:
        paths.reset_home()
    else:
        paths.configure_home(previous_home)


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
    return autopilot_controller.HostTaskResult(
        output="done\n\n=== TOOL EVIDENCE ===\nstep 1 tool=%s reason=test\nPASS" % tool,
        tools=(tool,),
        validation_attempted=task["kind"] == "validate",
        validation_passed=task["kind"] == "validate",
    )


def _review(_run, _issue):
    return {"decision": "complete", "reason": "verified", "tasks": []}


@pytest.mark.parametrize("mode", ["manual", "acceptEdits", "auto"])
@pytest.mark.parametrize("require_verifier", [None, "0", "true", "1"])
def test_start_verifier_preflight_uses_scratch_home_before_launch(monkeypatch, tmp_path, mode, require_verifier):
    import permission_modes

    (tmp_path / "permission_mode.json").write_text(json.dumps({"mode": mode}), encoding="utf-8")
    if require_verifier is None:
        monkeypatch.delenv("SONDER_AUTOPILOT_REQUIRE_VERIFIER", raising=False)
    else:
        monkeypatch.setenv("SONDER_AUTOPILOT_REQUIRE_VERIFIER", require_verifier)
    launched = []
    monkeypatch.setattr(server, "_launch_autopilot", lambda *args, **kwargs: launched.append(args) or True)
    before = dict(permission_modes._UNATTENDED)
    output = server.autopilot_start("Verify the scratch project")
    refused_start = mode != "auto" and require_verifier == "1"
    if not refused_start:
        assert output.startswith("autopilot started")
        assert len(launched) == 1
    else:
        assert output.startswith(
            "ERROR: no verifier tool is allowed unattended in mode %s; a validate task cannot pass" % mode
        )
        assert not launched
        assert autopilot_store.get_run() is None
    displays = [output, server.autopilot_status()]
    if not refused_start:
        displays.append(server.autopilot_status(autopilot_store.get_run()["id"]))
    for display in displays:
        if mode == "auto":
            assert "verifiers: allowed [test_run, workspace_run, script_run, build_run, run_code, lint_run, typecheck_run] refused []" in display
            assert "warning: validate tasks cannot pass unattended" not in display
        else:
            assert "verifiers: allowed [] refused [test_run, workspace_run, script_run, build_run, run_code, lint_run, typecheck_run]" in display
            assert "warning: validate tasks cannot pass unattended in mode %s" % mode in display
    assert permission_modes._UNATTENDED == before


@pytest.mark.parametrize("require_verifier", ["0", "1"])
def test_start_verifier_preflight_honors_explicit_tool_allow_rule(monkeypatch, tmp_path, require_verifier):
    import permission_modes

    (tmp_path / "permission_mode.json").write_text('{"mode": "manual"}', encoding="utf-8")
    monkeypatch.setenv("SONDER_AUTOPILOT_REQUIRE_VERIFIER", require_verifier)
    monkeypatch.setattr(permission_modes, "_rule_lookup", lambda name: {"action": "allow", "pattern": "test_run"} if name == "test_run" else None)
    monkeypatch.setattr(server, "_launch_autopilot", lambda *args, **kwargs: True)
    output = server.autopilot_start("Verify with the approved tool")
    assert output.startswith("autopilot started")
    assert "verifiers: allowed [test_run] refused [workspace_run" in output
    assert "warning: validate tasks cannot pass unattended" not in output


def test_waiting_start_warns_without_refusing_manual_mode(monkeypatch, tmp_path):
    """Advisory preflight must preserve synchronous execution as well as launch."""
    (tmp_path / "permission_mode.json").write_text('{"mode": "manual"}', encoding="utf-8")
    monkeypatch.delenv("SONDER_AUTOPILOT_REQUIRE_VERIFIER", raising=False)
    executed = []

    def execute(run_id, **_kwargs):
        executed.append(run_id)
        return autopilot_store.get_run(run_id)

    monkeypatch.setattr(server, "_execute_autopilot", execute)
    output = server.autopilot_start("Verify synchronously", wait=True)
    assert executed == [autopilot_store.get_run()["id"]]
    assert "verifiers: allowed [] refused [test_run, workspace_run" in output
    assert "warning: validate tasks cannot pass unattended in mode manual" in output


def _fake_reviewer_transport(monkeypatch, replies):
    prompts = []

    def generate(prompt):
        prompts.append(prompt)
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(server, "_serve_target", lambda *args: ("fake-local", False, False, "code"))
    monkeypatch.setattr(server, "_bridge_provider_for_tier", lambda _: "ollama")
    monkeypatch.setattr(server, "_build_system", lambda *args, **kwargs: "system")
    monkeypatch.setattr(server, "_make_tier_generate", lambda *args, **kwargs: generate)
    return prompts


@pytest.mark.parametrize("reply", ["reviewer-garbage", '{"decision": "bogus"}'])
def test_reviewer_invalid_json_or_schema_repairs_once_then_coerces(monkeypatch, caplog, reply):
    prompts = _fake_reviewer_transport(monkeypatch, [reply, reply])
    result = server._autopilot_review_model({"tier": "code", "plan": []}, "host completion gates passed")
    assert result["decision"] == "continue"
    assert result.get("tasks", []) == []
    assert len(prompts) == 2
    assert "HOST SCHEMA ERROR" not in prompts[0]
    assert "HOST SCHEMA ERROR" in prompts[1]
    assert reply in caplog.text


def test_reviewer_accepts_repaired_response(monkeypatch):
    prompts = _fake_reviewer_transport(monkeypatch, ["garbage", '{"decision": "pause", "reason": "inspect"}'])
    result = server._autopilot_review_model({"tier": "code", "plan": []}, "checkpoint")
    assert result["decision"] == "pause"
    assert len(prompts) == 2


def test_reviewer_transport_error_is_not_schema_coercion(monkeypatch):
    from sonder_runtime.adapters.model_transport import ModelCallError

    error = ModelCallError("provider_unavailable", "health probe timed out")
    prompts = _fake_reviewer_transport(monkeypatch, [error])
    with pytest.raises(ModelCallError):
        server._autopilot_review_model({"tier": "code", "plan": []}, "checkpoint")
    assert len(prompts) == 1


def test_planner_invalid_json_still_raises_after_one_repair(monkeypatch):
    prompts = _fake_reviewer_transport(monkeypatch, ["garbage", "garbage"])
    with pytest.raises(ValueError, match="planner model failed"):
        server._autopilot_plan_model({"tier": "code", "plan": []})
    assert len(prompts) == 2


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


def test_planner_truncates_overlong_plan_instead_of_failing_the_run(monkeypatch):
    # Regression (2026-07-13): a local planner that emitted more tasks than the
    # (adaptive) initial_limit raised "initial plan exceeds the N-task adaptive
    # planning limit", the single retry over-planned again, and the ENTIRE
    # autonomous run failed before one task ran. The surplus must be trimmed
    # (adaptive review can add tasks back), not fail the run.
    overlong = {
        "summary": "too many tasks",
        "success_criteria": ["evidence present"],
        "tasks": [
            {"title": "T1", "kind": "inspect", "instruction": "a"},
            {"title": "T2", "kind": "inspect", "instruction": "b"},
            {"title": "T3", "kind": "research", "instruction": "c"},
            {"title": "T4", "kind": "research", "instruction": "d"},
            {"title": "T5", "kind": "validate", "instruction": "e"},
        ],
    }

    seen = {}

    def fake_json(run, role, prompt, validator):
        # max_tasks=4 -> reserve=1 -> initial_limit=3; this 5-task plan is over.
        payload = dict(overlong)
        payload["tasks"] = list(overlong["tasks"])
        validator(payload)  # must NOT raise; must trim payload["tasks"] in place
        seen["after_validate"] = len(payload["tasks"])
        return payload

    monkeypatch.setattr(server, "_autopilot_json_model", fake_json)

    payload = server._autopilot_plan_model({
        "objective": "count files", "project": "demo", "policy": "observe",
        "allow_web": False, "adaptive": True, "max_tasks": 4,
        "max_replans": 2, "tier": "code",
    })

    # validator trimmed the raw plan to the initial limit (3) rather than raising.
    assert seen["after_validate"] == 3
    # normalize_plan (run by the controller) still guarantees a grounded
    # validate task survives the truncation.
    normalized = autopilot_controller.normalize_plan(payload, "count files", 4)
    assert any(t["kind"] == "validate" for t in normalized["tasks"])
    assert len(normalized["tasks"]) <= 4


def test_planner_still_rejects_implementation_under_observe_policy(monkeypatch):
    # Truncation must not weaken the observe-policy safety check: an implement
    # task under observe policy is still a hard failure, not silently trimmed.
    bad = {
        "summary": "implements under observe",
        "success_criteria": ["evidence present"],
        "tasks": [
            {"title": "Edit", "kind": "implement", "instruction": "change code"},
            {"title": "Validate", "kind": "validate", "instruction": "verify"},
        ],
    }

    def fake_json(run, role, prompt, validator):
        validator(dict(bad, tasks=list(bad["tasks"])))
        return bad

    monkeypatch.setattr(server, "_autopilot_json_model", fake_json)

    with pytest.raises(ValueError, match="observe policy"):
        server._autopilot_plan_model({
            "objective": "x", "project": "demo", "policy": "observe",
            "allow_web": False, "adaptive": True, "max_tasks": 4,
            "max_replans": 2, "tier": "code",
        })


def test_reviewer_receives_checkpoint_evidence_and_continue_decision(monkeypatch):
    captured = {}

    def fake_json(_factory, prompt, validator, _extract):
        captured.update({"prompt": prompt})
        payload = {
            "decision": "continue",
            "reason": "plan remains correct",
            "pending_assessment": [
                {"id": "task-02", "verdict": "keep", "reason": "still required"},
            ],
        }
        validator(payload)
        return payload

    monkeypatch.setattr(autopilot_controller, "review_json_model", fake_json)
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
    assert '"evidence_actions": ["file_read: inspect"]' in captured["prompt"]
    assert '"instruction": "Run focused tests"' in captured["prompt"]
    assert '"pending_assessment"' in captured["prompt"]
    assert "complete|continue|retry|replan|pause" in captured["prompt"]


def test_reviewer_rejects_continue_when_pending_assessment_is_stale(monkeypatch):
    def fake_json(_factory, _prompt, validator, _extract):
        payload = {
            "decision": "continue",
            "reason": "nothing else needed",
            "pending_assessment": [
                {"id": "task-02", "verdict": "stale", "reason": "already exists"},
            ],
        }
        validator(payload)
        return payload

    monkeypatch.setattr(autopilot_controller, "review_json_model", fake_json)
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


def test_reviewer_replan_with_nothing_stale_is_coerced_to_continue(monkeypatch):
    # Regression (2026-07-13): a local reviewer that said "replan" while marking
    # every pending task "keep" (nothing stale to replace) previously raised
    # "replan requires at least one stale pending task", the retry repeated the
    # inconsistency, and the ENTIRE autonomous run failed mid-execution with
    # valid pending tasks still queued. A replan with nothing to act on means
    # the same as continue -- coerce it, do not fail the run.
    def fake_json(_factory, _prompt, validator, _extract):
        payload = {
            "decision": "replan",
            "reason": "the plan still looks fine actually",
            "pending_assessment": [
                {"id": "task-02", "verdict": "keep", "reason": "still needed"},
            ],
            "tasks": [],
        }
        validator(payload)  # must NOT raise; must coerce decision in place
        return payload

    monkeypatch.setattr(autopilot_controller, "review_json_model", fake_json)
    result = server._autopilot_review_model({
        "objective": "adapt", "max_tasks": 6, "max_replans": 1,
        "plan": [
            {
                "id": "task-01", "kind": "inspect", "title": "Inspect",
                "instruction": "Inspect", "status": "passed", "output": "found",
            },
            {
                "id": "task-02", "kind": "validate", "title": "Validate",
                "instruction": "Run checks", "status": "pending", "output": "",
            },
        ],
    }, "adaptive checkpoint after task-01")

    assert result["decision"] == "continue"


def test_reviewer_drops_assessments_of_non_pending_tasks_instead_of_failing(monkeypatch):
    # Regression (2026-07-13): a local reviewer assessed already-PASSED tasks
    # (task-01, task-02) as pending. The validator raised "adaptive review
    # assessed unknown pending tasks: task-01, task-02" and, after a fruitless
    # retry, the entire autonomous run failed with valid pending tasks still
    # queued. Non-pending / malformed / duplicate assessments must be dropped,
    # not fatal -- the controller only acts on "stale" verdicts for genuinely
    # pending tasks.
    captured = {}

    def fake_json(_factory, _prompt, validator, _extract):
        payload = {
            "decision": "continue",
            "reason": "looks fine",
            "pending_assessment": [
                {"id": "task-01", "verdict": "keep", "reason": "already passed"},
                {"id": "task-02", "verdict": "keep", "reason": "already passed"},
                {"id": "task-03", "verdict": "keep", "reason": "still needed"},
                {"id": "task-03", "verdict": "keep", "reason": "dup"},          # duplicate
                {"id": "task-04", "verdict": "banana", "reason": "junk verdict"},  # bad verdict
                "not-an-object",                                                 # non-dict
            ],
            "tasks": [],
        }
        validator(payload)  # must NOT raise
        captured.update(payload)
        return payload

    monkeypatch.setattr(autopilot_controller, "review_json_model", fake_json)
    result = server._autopilot_review_model({
        "objective": "count files", "max_tasks": 6, "max_replans": 1,
        "plan": [
            {"id": "task-01", "kind": "inspect", "title": "Inspect",
             "instruction": "i", "status": "passed", "output": "done"},
            {"id": "task-02", "kind": "research", "title": "Count",
             "instruction": "c", "status": "passed", "output": "done"},
            {"id": "task-03", "kind": "report", "title": "Report",
             "instruction": "r", "status": "pending", "output": ""},
            {"id": "task-04", "kind": "validate", "title": "Validate",
             "instruction": "v", "status": "pending", "output": ""},
        ],
    }, "adaptive checkpoint after task-02")

    assert result["decision"] == "continue"
    assessed_ids = {a["id"] for a in captured["pending_assessment"]}
    # Only the genuinely-pending tasks survive; passed tasks are dropped.
    assert assessed_ids == {"task-03", "task-04"}
    # task-04 had a junk verdict -> defaulted to keep, not dropped or fatal.
    verdicts = {a["id"]: a["verdict"] for a in captured["pending_assessment"]}
    assert verdicts == {"task-03": "keep", "task-04": "keep"}


def test_reviewer_fills_omitted_pending_assessments_as_keep(monkeypatch):
    captured = {}

    def fake_json(_factory, _prompt, validator, _extract):
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

    monkeypatch.setattr(autopilot_controller, "review_json_model", fake_json)
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
    assert {"data_inspect", "repo_status", "repo_diff"}.issubset(
        server._AUTOPILOT_OBSERVE_TOOLS
    )


def test_agent_host_allowlist_rejects_model_tool_expansion(monkeypatch, without_standing):
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
    assert without_standing(output).startswith("stopped after host denial")
    assert dispatched == []
    assert "outside this autonomous run's allowlist" in output


def test_http_autopilot_status_is_safe_but_lifecycle_changes_require_developer():
    assert sonder_serve._dangerous_http_slash("/autopilot") is False
    assert sonder_serve._dangerous_http_slash("/autopilot status auto-1") is False
    assert sonder_serve._dangerous_http_slash("/autopilot run change files") is True
    assert sonder_serve._dangerous_http_slash("/auto cancel auto-1") is True


def test_served_autopilot_binds_an_opaque_account_scope(monkeypatch):
    captured = {}
    context = {"account": {"username": "alice"}, "api_key": "", "mode": "account"}
    monkeypatch.setattr(
        server, "control_command",
        lambda prompt, **kwargs: captured.update(kwargs) or "ok",
    )
    assert sonder_serve._handle_slash("/autopilot status", context=context) == "ok"
    assert captured["autopilot_request_owner"] == sonder_serve._task_account_scope(context)
    assert captured["autopilot_request_owner"].startswith("ta-")


def test_diagnostics_manifest_and_improvement_expose_autopilot(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setattr(server, "_get", lambda path: {"models": []})
    assert "autopilot:" in server.diagnostics()
    assert "execution routing: host-gated" in server.diagnostics()
    assert "autopilot_start" in server.tool_manifest()
    assert "/autopilot" in server.command_registry_list("agents")
    report = server.improvement_report_data()
    assert "autopilot" in report


def test_cancel_of_an_active_run_acknowledges_the_pending_request():
    # Live repro: `/autopilot cancel <id>` during a task echoed the run as
    # "status/phase: running / execute" with no acknowledgment, which reads
    # as if the cancel was ignored; the terminal state arrives only at the
    # next host checkpoint.
    import os

    run = autopilot_store.create_run("cancel ack drill")
    assert autopilot_store.claim_run(run["id"], "owner-a", owner_pid=os.getpid())
    output = server._autopilot_cancel(run["id"])
    assert output.startswith("autopilot cancellation requested")
    assert "next host checkpoint" in output
    assert "id: %s" % run["id"] in output


def test_cancel_of_an_idle_run_reports_it_cancelled():
    run = autopilot_store.create_run("idle cancel drill")
    output = server._autopilot_cancel(run["id"])
    assert output.startswith("autopilot cancelled")
    assert "cancelled / cancelled" in output


def test_cancel_of_a_missing_run_is_rejected():
    output = server._autopilot_cancel("auto-missing")
    assert output.startswith("autopilot request rejected")
