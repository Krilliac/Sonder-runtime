import execution_status
import server


def test_fleet_execution_contract_distinguishes_lanes_running_and_queued():
    result = execution_status.from_fleet_snapshot({
        "active_agents": 5,
        "running_agents": 3,
        "queued_agents": 2,
        "active_model_calls": 2,
    })

    assert result == {
        "known": True,
        "running_lanes": 2,
        "running_agents": 3,
        "queued_agents": 2,
        "active_agents": 5,
        "semantics": "fleet model-call lanes and durable fleet agents",
        "error": "",
    }


def test_incomplete_or_inconsistent_snapshot_is_unknown_not_false_zero():
    assert execution_status.from_fleet_snapshot({})["known"] is False
    assert execution_status.from_fleet_snapshot({
        "active_agents": 5,
        "running_agents": 1,
        "queued_agents": 1,
        "active_model_calls": 0,
    })["known"] is False


def test_server_contract_reuses_the_status_endpoint_agent_snapshot(monkeypatch):
    monkeypatch.setattr(
        server.master_orchestrator,
        "snapshot",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("duplicate read")),
    )
    result = server.execution_status_data({
        "active_agents": 2,
        "running_agents": 1,
        "queued_agents": 1,
        "active_model_calls": 1,
    })

    assert result["known"] is True
    assert result["running_lanes"] == 1
