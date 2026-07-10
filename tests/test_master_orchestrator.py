import master_orchestrator


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
    monkeypatch.delenv("TRILOBITE_MAX_AGENTS", raising=False)

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
    monkeypatch.setenv("TRILOBITE_MAX_AGENTS", "24")

    result = master_orchestrator.run_delegated(
        "fan out",
        worker_fn=lambda prompt: "ok",
        audit_fn=lambda prompt: "merged",
        agents=99,
    )

    assert master_orchestrator.max_agents() == 24
    assert len(result["agents"]) == 24
