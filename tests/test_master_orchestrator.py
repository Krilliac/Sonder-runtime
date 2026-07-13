import importlib
import master_orchestrator
import threading
import time


def setup_function():
    master_orchestrator.reset_for_tests()


def test_evidence_gate_rejects_repo_inspection_when_tools_unavailable():
    task = "Repository: D:\\SparkEngine. Review current uncommitted files using local file-reading tools."

    assert master_orchestrator.evidence_gate(task, tools_available=False) == master_orchestrator.EVIDENCE_REQUIRED
    assert master_orchestrator.evidence_gate(task, tools_available=True) == ""


def test_evidence_gate_allows_embedded_source_excerpt():
    task = "Review the current file. Source excerpt:\n```cpp\nint answer() { return 42; }\n```"

    assert master_orchestrator.evidence_gate(task) == ""


def test_greenfield_design_is_not_treated_as_repository_inspection():
    task = "Design and implement a C++ 2.5D isometric RPG from scratch."

    assert not master_orchestrator.requires_repository_tools(task)


def test_delegated_prompts_disclose_no_tool_access_and_demand_evidence():
    prompt = master_orchestrator._subtask_prompts("compare these excerpts", 1)[0]

    assert "no filesystem, shell, web" in prompt
    assert "Quote the exact supporting excerpt" in prompt
    assert "EVIDENCE_REQUIRED" in prompt


def test_repository_prompts_require_guarded_read_tools():
    prompt = master_orchestrator._subtask_prompts(
        "Repository: D:\\SparkEngine. Inspect current files.",
        1,
        tool_access=True,
    )[0]

    assert "guarded read-only file tools" in prompt
    assert "never request write/edit/delete tools" in prompt


def test_run_inline_tracks_master_agent():
    result = master_orchestrator.run_inline("say hi", lambda prompt: "done: " + prompt)
    snap = master_orchestrator.snapshot()

    assert result["mode"] == "inline"
    assert result["output"] == "done: say hi"
    assert any(a["id"] == result["master_id"] and a["status"] == "done" for a in snap["agents"])


def test_run_delegated_tracks_children_and_audit():
    def worker(prompt):
        return "worker saw " + prompt.splitlines()[-1]

    def audit(prompt):
        assert "worker saw" in prompt
        assert "Discard invented files, symbols, APIs" in prompt
        return "merged"

    result = master_orchestrator.run_delegated(
        "compare options",
        worker_fn=worker,
        audit_fn=audit,
        agents=2,
    )
    snap = master_orchestrator.snapshot()

    assert result["mode"] == "delegated"
    assert result["output"] == "merged"
    assert len(result["agents"]) == 2
    assert snap["active_agents"] == 0
    assert snap["tokens_in"] > 0
    assert snap["latest_master_result"] == "merged"
    assert "latest completed master result:\nmerged" in master_orchestrator.format_snapshot(snap)


def test_all_failed_workers_fail_master_and_skip_audit():
    audited = []

    def fail(_prompt):
        raise RuntimeError("backend unavailable")

    result = master_orchestrator.run_delegated(
        "compare options",
        worker_fn=fail,
        audit_fn=lambda prompt: audited.append(prompt) or "must not run",
        agents=2,
    )
    snap = master_orchestrator.snapshot()
    master = next(row for row in snap["agents"] if row["role"] == "master")
    children = [row for row in snap["agents"] if row["role"] == "agent"]

    assert result["outputs"] == []
    assert "all delegated workers failed" in result["output"]
    assert master["status"] == "failed"
    assert {row["status"] for row in children} == {"failed"}
    assert audited == []


def test_partial_fleet_audits_successful_outputs_only():
    audited = []

    def worker(prompt):
        if "subagent 1/2" in prompt:
            raise RuntimeError("first worker failed")
        return "usable worker result"

    def audit(prompt):
        audited.append(prompt)
        assert "usable worker result" in prompt
        assert "first worker failed" not in prompt
        return "merged success"

    result = master_orchestrator.run_delegated(
        "compare options", worker_fn=worker, audit_fn=audit, agents=2,
    )
    snap = master_orchestrator.snapshot()

    assert result["output"] == "merged success"
    assert len(result["outputs"]) == 1
    assert len(audited) == 1
    assert any(row["status"] == "failed" for row in snap["agents"])
    assert any(
        row["role"] == "master" and row["status"] == "done"
        for row in snap["agents"]
    )


def test_repository_delegation_refuses_outputs_without_tool_ledger(monkeypatch):
    monkeypatch.setattr(
        master_orchestrator,
        "_repository_worker",
        lambda prompt: "I inspected it and everything passes.",
    )
    audited = []

    result = master_orchestrator.run_delegated(
        "Repository: D:\\SparkEngine. Inspect current files.",
        worker_fn=lambda prompt: "unused",
        audit_fn=lambda prompt: audited.append(prompt) or "should not run",
        agents=2,
    )

    assert result["output"] == master_orchestrator.EVIDENCE_REQUIRED
    assert result["outputs"] == []
    assert audited == []


def test_run_delegated_default_cap_allows_sixteen_agents(monkeypatch):
    monkeypatch.delenv("SONDER_MAX_AGENTS", raising=False)

    result = master_orchestrator.run_delegated(
        "fan out",
        worker_fn=lambda prompt: "ok",
        audit_fn=lambda prompt: "merged",
        agents=99,
    )

    assert master_orchestrator.max_agents() == master_orchestrator.hardware_max_agents()
    assert len(result["agents"]) == master_orchestrator.hardware_max_agents()


def test_fleet_keywords_request_hardware_fanout():
    assert master_orchestrator.requests_fleet("spawn as many parallel agents as possible")
    assert master_orchestrator.requests_fleet("run a fleet workflow")
    assert not master_orchestrator.requests_fleet("review this one file")


def test_run_delegated_agent_cap_is_configurable(monkeypatch):
    monkeypatch.setenv("SONDER_MAX_AGENTS", "24")

    result = master_orchestrator.run_delegated(
        "fan out",
        worker_fn=lambda prompt: "ok",
        audit_fn=lambda prompt: "merged",
        agents=99,
    )

    assert master_orchestrator.max_agents() == 24
    assert len(result["agents"]) == 24


def test_capacity_separates_agent_ceiling_from_memory_safe_worker_slots(monkeypatch):
    gib = 1024 ** 3
    monkeypatch.delenv("SONDER_MAX_AGENTS", raising=False)
    monkeypatch.delenv("SONDER_PARALLEL_WORKERS", raising=False)
    monkeypatch.setattr(master_orchestrator.os, "cpu_count", lambda: 16)
    monkeypatch.setattr(
        master_orchestrator, "physical_memory_bytes", lambda: (16 * gib, 2 * gib)
    )

    low = master_orchestrator.capacity(32)

    assert low["agent_ceiling"] == 32
    assert low["requested_agents"] == 32
    assert low["worker_slots"] == 1
    assert low["source"] == "auto"

    monkeypatch.setattr(
        master_orchestrator, "physical_memory_bytes", lambda: (16 * gib, 10 * gib)
    )
    healthy = master_orchestrator.capacity(32)
    assert healthy["worker_slots"] == 4


def test_parallel_worker_override_is_explicit_and_bounded(monkeypatch):
    gib = 1024 ** 3
    monkeypatch.setenv("SONDER_PARALLEL_WORKERS", "6")
    monkeypatch.setattr(master_orchestrator.os, "cpu_count", lambda: 16)
    monkeypatch.setattr(
        master_orchestrator, "physical_memory_bytes", lambda: (16 * gib, 2 * gib)
    )

    report = master_orchestrator.capacity(10)

    assert report["worker_slots"] == 6
    assert report["source"] == "SONDER_PARALLEL_WORKERS"
    assert "concurrent worker slots: 6" in master_orchestrator.format_capacity(report)


def test_delegated_fleet_limits_actual_concurrency(monkeypatch):
    monkeypatch.setattr(master_orchestrator, "parallel_worker_slots", lambda requested: 2)
    lock = threading.Lock()
    current = {"active": 0, "maximum": 0}

    def worker(prompt):
        with lock:
            current["active"] += 1
            current["maximum"] = max(current["maximum"], current["active"])
        # Leave enough overlap for the process-shared SQLite start transition;
        # the assertion concerns model-call concurrency, not ledger connection time.
        time.sleep(0.12)
        with lock:
            current["active"] -= 1
        return "ok"

    result = master_orchestrator.run_delegated(
        "fan out", worker_fn=worker, audit_fn=lambda prompt: "merged", agents=6,
    )

    assert len(result["agents"]) == 6
    assert result["worker_slots"] == 2
    assert current["maximum"] == 2


def test_cancel_master_skips_queued_workers_and_discards_running_result(monkeypatch):
    monkeypatch.setattr(master_orchestrator, "parallel_worker_slots", lambda requested: 1)
    started = threading.Event()
    release = threading.Event()
    calls = []
    audited = []
    result_box = {}

    def worker(prompt):
        calls.append(prompt)
        started.set()
        assert release.wait(2)
        return "late result"

    def run():
        result_box["result"] = master_orchestrator.run_delegated(
            "cancel fleet",
            worker_fn=worker,
            audit_fn=lambda prompt: audited.append(prompt) or "merged",
            agents=4,
        )

    thread = threading.Thread(target=run)
    thread.start()
    assert started.wait(2)
    snap = master_orchestrator.snapshot(include_finished=False, limit=20)
    master_id = next(row["id"] for row in snap["agents"] if row["role"] == "master")

    canceled = master_orchestrator.request_cancel(master_id)
    release.set()
    thread.join(3)

    assert not thread.is_alive()
    assert canceled["matched"] == 5
    assert canceled["queued"] == 3
    assert canceled["running"] == 2
    assert canceled["model_calls"] == 1
    assert result_box["result"]["output"] == "CANCELLED"
    assert len(calls) == 1
    assert audited == []
    final = master_orchestrator.snapshot(limit=20)
    assert final["active_agents"] == 0
    assert {row["status"] for row in final["agents"]} == {"cancelled"}


def test_cancelled_queued_worker_cannot_transition_to_running():
    calls = []
    agent_id = master_orchestrator._new_agent("agent", "queued work")
    master_orchestrator.request_cancel(agent_id)

    output = master_orchestrator._run_worker(
        agent_id, "prompt", lambda prompt: calls.append(prompt) or "unexpected",
    )

    assert output == "CANCELLED"
    assert calls == []
    row = master_orchestrator.snapshot(limit=5)["agents"][0]
    assert row["status"] == "cancelled"


def test_child_created_after_parent_cancellation_inherits_cancel_state():
    master_id = master_orchestrator._new_agent("master", "parent")
    master_orchestrator.request_cancel(master_id)

    child_id = master_orchestrator._new_agent(
        "agent", "late child", parent_id=master_id,
    )

    child = next(
        row for row in master_orchestrator.snapshot(limit=5)["agents"]
        if row["id"] == child_id
    )
    assert child["status"] == "cancelled"
    assert child["cancel_requested"] is True


def test_snapshot_active_count_is_not_clipped_by_display_limit():
    for index in range(25):
        master_orchestrator._new_agent("agent", "task %d" % index)

    snap = master_orchestrator.snapshot(include_finished=False, limit=5)

    assert snap["active_agents"] == 25
    assert snap["total_listed"] == 5


def test_hot_reload_preserves_owner_and_active_execution_state():
    owner_id = master_orchestrator._OWNER_ID
    agent_id = master_orchestrator._new_agent("agent", "survive reload")

    reloaded = importlib.reload(master_orchestrator)
    snap = reloaded.snapshot(include_finished=False, limit=5)

    assert reloaded._OWNER_ID == owner_id
    assert any(row["id"] == agent_id for row in snap["agents"])


def test_hot_reload_preserves_worker_failure_sentinel_identity():
    sentinel = master_orchestrator._WORKER_FAILED

    reloaded = importlib.reload(master_orchestrator)

    assert reloaded._WORKER_FAILED is sentinel


def test_inline_and_audit_bind_current_ledger_agent():
    inline_ids = []
    audit_ids = []

    inline = master_orchestrator.run_inline(
        "inline",
        lambda prompt: inline_ids.append(
            getattr(master_orchestrator._WORKER_LOCAL, "agent_id", None)
        ) or "done",
    )
    delegated = master_orchestrator.run_delegated(
        "delegate",
        worker_fn=lambda prompt: "worker",
        audit_fn=lambda prompt: audit_ids.append(
            getattr(master_orchestrator._WORKER_LOCAL, "agent_id", None)
        ) or "merged",
        agents=1,
    )

    assert inline_ids == [inline["master_id"]]
    assert audit_ids == [delegated["master_id"]]
