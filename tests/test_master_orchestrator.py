import master_orchestrator


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


def test_run_delegated_default_cap_allows_sixteen_agents(monkeypatch):
    monkeypatch.delenv("TRILOBITE_MAX_AGENTS", raising=False)

    result = master_orchestrator.run_delegated(
        "fan out",
        worker_fn=lambda prompt: "ok",
        audit_fn=lambda prompt: "merged",
        agents=99,
    )

    assert master_orchestrator.max_agents() == 16
    assert len(result["agents"]) == 16


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
