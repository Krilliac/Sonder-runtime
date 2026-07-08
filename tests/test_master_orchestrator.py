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
