from __future__ import annotations

from sonder_runtime.bootstrap import http_command_narration as narration


def test_status_and_cancel_shapes_remain_synchronous():
    assert not narration.is_long_command("autopilot_status")
    assert not narration.is_long_command("autopilot_start", {"action": "status"})
    assert not narration.is_long_command("master_orchestrate", {"mode": "ask"})
    assert narration.is_long_command("autopilot_start", {"objective": "build"})
    assert not narration.is_long_command("master_orchestrate", {"task": "help"})
    assert not narration.is_long_command("autopilot", {"action": "resume", "objective": "existing-run"})


def test_ack_is_deterministic_and_uses_existing_arguments():
    args = {"task": "build the app", "mode": "fleet", "agents": 4, "worker_cap": 2}
    first = narration.command_ack({}, "master_orchestrate", args, project="D:/demo", reason="explicit fleet")
    second = narration.command_ack({}, "master_orchestrate", args, project="D:/demo", reason="explicit fleet")
    assert first == second
    assert "build the app" in first
    assert "a fleet" in first
    assert "D:/demo" in first
    assert "explicit fleet" in first


def test_run_command_uses_deferred_runner_and_persists_ack_and_links(monkeypatch):
    calls = []

    class Store:
        def set_narration(self, run_id, **kwargs):
            calls.append(("ack", run_id, kwargs))

        def link_run(self, run_id, kind, child_id):
            calls.append(("link", run_id, kind, child_id))

    class Tracker:
        def current_response_id(self):
            return "response-1"

    class Runner:
        def run(self, principal, call, **kwargs):
            calls.append(("run", principal, kwargs))
            assert kwargs["wait_seconds"] == 0
            kwargs["on_admitted"]("wr-1")
            call()
            return type("Outcome", (), {"run_id": "wr-1"})()

    result = narration.run_command(
        runner=Runner(), principal="owner", call=lambda: "done",
        classify=lambda value: ("returned", value), store=Store(),
        tracker=Tracker(), acknowledgement="I will work on it.",
    )
    assert result.work_run_id == "wr-1"
    assert calls[0][0] == "run"
    assert calls[1][0:2] == ("ack", "wr-1")
    assert calls[1][2]["acknowledgement"].startswith("I will work on it.")


def test_error_and_unknown_results_do_not_become_successful_returns():
    assert narration.classify_result("ERROR: provider offline")[0] == "failed"
    assert narration.classify_result("refused by policy")[0] == "refused"
    assert narration.classify_result("")[0] == "unknown"
    assert narration.classify_result({"status": "unrecognized"})[0] == "unknown"


def test_non_work_dispatch_does_not_admit_background_work():
    calls = []
    result = narration.dispatch({}, "status", {}, lambda: calls.append("status") or "ready",
                                runner=None, principal="alice", store=None, tracker=None)
    assert result == "ready" and calls == ["status"]
