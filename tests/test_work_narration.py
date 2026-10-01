import time

from sonder_runtime.domain import work_narration
from sonder_runtime.domain.work_narration import (
    acknowledgement, activity_progress, autopilot_progress, fanout_progress,
    fleet_progress, progress,
)


def test_acknowledgement_fleet_is_deterministic_and_actionable():
    args = dict(goal="index the project", mode="fleet", project="D:/project",
                tier="local", host="node1", reason="three independent tasks",
                agents=5, worker_slots=2, per_agent_seconds=7,
                tasks=("scan", "test"), watch="/v1/sonder/status",
                cancel="cancel run-1")
    first = acknowledgement(**args)
    assert first == acknowledgement(**args)
    assert "index the project" in first
    assert "fleet of 5 agents on 2 worker slots" in first
    assert "about 21s" in first
    assert "watch /v1/sonder/status" in first
    assert "cancel with cancel run-1" in first


def test_acknowledgement_does_not_invent_estimate_or_plain_chat_work():
    text = acknowledgement("answer this", "single", per_agent_seconds=9)
    assert "estimated" not in text
    assert "single agent" in text
    assert progress(run_id="plain-chat") == []


def test_fleet_groups_root_children_and_emits_final_evidence():
    snapshot = {
        "agents": [
            {"id": "root", "status": "finished", "updated_ts": 30,
             "finished_ts": 30, "summary": "created report",
             "host_receipt": "receipt-2-checks"},
            {"id": "a", "parent_id": "root", "status": "finished",
             "updated_ts": 20, "summary": "scanned files"},
            {"id": "b", "parent_id": "root", "status": "running",
             "updated_ts": 25, "activity": "running tests"},
            {"id": "unrelated", "parent_id": "other", "status": "failed",
             "updated_ts": 100, "error": "should not appear"},
        ]
    }
    rows = fleet_progress(snapshot, "root")
    assert any("1/2 done, 1 running" in row["text"] for row in rows)
    final = [row for row in rows if row["final"]]
    assert final and "receipt-2-checks" in final[-1]["text"]
    assert all("should not appear" not in row["text"] for row in rows)


def test_autopilot_events_preserve_order_and_final_summary():
    snapshot = {
        "run": {"id": "auto-1", "status": "completed", "updated_ts": 30,
                "finished_ts": 30, "summary": "published output",
                "host_receipt": "receipt-artifact"},
        "events": [
            {"event_id": 1, "run_id": "auto-1", "ts": 10,
             "kind": "task_started", "message": "plan"},
            {"event_id": 2, "run_id": "auto-1", "ts": 20,
             "kind": "validation", "message": "validated"},
        ],
    }
    rows = autopilot_progress(snapshot)
    assert [row["text"] for row in rows[:2]] == ["plan", "validated"]
    assert rows[-1]["final"] and "receipt-artifact" in rows[-1]["text"]


def test_autopilot_uses_events_attached_to_each_run_and_deduplicates_top_level():
    snapshot = {
        "runs": [{"id": "auto-2", "status": "running", "events": [
            {"event_id": 7, "run_id": "auto-2", "ts": 10,
             "kind": "task_started", "message": "inspect"},
            {"event_id": 8, "run_id": "auto-2", "ts": 20,
             "kind": "retry", "message": "busy; retrying"},
        ]}],
        "events": [
            {"event_id": 7, "run_id": "auto-2", "ts": 10,
             "kind": "task_started", "message": "inspect"},
        ],
    }
    rows = autopilot_progress(snapshot)
    assert [row["text"] for row in rows] == ["inspect", "Retry: busy; retrying"]


def test_rate_limit_keeps_terminal_update_inside_a_busy_window():
    rows = autopilot_progress({"run": {"id": "auto-3", "status": "running"},
                               "events": [
                                   {"event_id": 1, "ts": 10, "kind": "running", "message": "a"},
                                   {"event_id": 2, "ts": 15, "kind": "running", "message": "b"},
                                   {"event_id": 3, "ts": 19, "kind": "task_fail", "message": "failed"},
                               ]})
    assert [row["text"] for row in rows] == ["a", "Task failed: failed"]


def test_activity_suppresses_model_call_noise_but_keeps_validation_and_failure():
    rows = activity_progress({"active": [{"id": "resp-2", "events": [
        {"seq": 1, "ts": 1, "kind": "model_call", "title": "model"},
        {"seq": 2, "ts": 2, "kind": "validation", "title": "validation passed"},
        {"seq": 3, "ts": 3, "kind": "response_error", "summary": {"available": True, "text": "failed"}},
    ]}]})
    assert [row["text"] for row in rows] == ["validation passed", "failed"]
    assert rows[-1]["final"] is True


def test_activity_keeps_failed_model_call_reason_as_terminal_progress():
    rows = activity_progress({"active": [{"id": "resp-fail", "events": [
        {"seq": 1, "ts": 1, "kind": "model_call", "phase": "error",
         "summary": {"available": True, "text": "provider busy; retrying"}},
    ]}]})
    assert len(rows) == 1
    assert rows[0]["kind"] == "failed"
    assert rows[0]["final"] is True
    assert "provider busy" in rows[0]["text"]


def test_fleet_aggregate_id_is_stable_across_heartbeat_timestamps():
    base = {"agents": [
        {"id": "master", "status": "running", "updated_ts": 10,
         "requested_agents": 1, "worker_slots": 1},
        {"id": "child", "parent_id": "master", "status": "running", "updated_ts": 10},
    ]}
    first = fleet_progress(base, "master")[-1]
    base["agents"][0]["updated_ts"] = 20
    base["agents"][1]["updated_ts"] = 20
    second = fleet_progress(base, "master")[-1]
    assert first["id"] == second["id"]


def test_fleet_child_window_projects_progress_without_inventing_master_result():
    rows = fleet_progress({"agents": [
        {"id": "child-1", "parent_id": "master-1", "status": "running", "updated_ts": 5},
    ], "requested_agents": 1, "worker_slots_total": 1}, "master-1")
    assert len(rows) == 1
    assert "0/1 done, 1 running" in rows[0]["text"]
    assert "no output recorded" not in rows[0]["text"]


def test_progress_redacts_credentials_in_durable_event_text():
    rows = autopilot_progress({"run": {"id": "auto-secret", "status": "running"},
                               "events": [{"event_id": 1, "ts": 1, "kind": "retry",
                                            "message": "TOKEN=abc Bearer xyz https://u:p@example.test"}]})
    assert "abc" not in rows[0]["text"]
    assert "Bearer xyz" not in rows[0]["text"]
    assert "u:p@" not in rows[0]["text"]
    assert "<redacted>" in rows[0]["text"]


def test_progress_redacts_a_credential_nested_in_another_assignment():
    # The shape a value-consuming rewrite of the credential pattern leaks:
    # `x=` is not a credential, but its value holds one.
    rows = autopilot_progress({"run": {"id": "auto-nested", "status": "running"},
                               "events": [{"event_id": 1, "ts": 1, "kind": "retry",
                                            "message": "env x=password=hunter2 secret_key=k1 pwd2: p3"}]})
    text = rows[0]["text"]
    assert "hunter2" not in text and "k1" not in text and "p3" not in text
    assert "x=password=<redacted>" in text


def test_redaction_stays_linear_on_inputs_that_made_it_quadratic():
    # ~30k chars each. The single-regex credential pattern rescanned a run
    # from every position inside it and took seconds on each of these
    # (CodeQL py/polynomial-redos); a linear pass takes milliseconds, so the
    # bound separates the two by orders of magnitude, not by a hair.
    for payload in ("-" * 30_000, "pwd" * 10_000, "eyJ-" * 7_500):
        started = time.perf_counter()
        work_narration._text(payload, 240)
        assert time.perf_counter() - started < 2.0, payload[:8]


def test_progress_is_bounded_and_rate_limits_nonterminal_events():
    snapshot = {"events": [
        {"ts": 10, "kind": "running", "message": "one"},
        {"ts": 15, "kind": "running", "message": "two"},
        {"ts": 20, "kind": "failed", "message": "failed"},
    ], "run": {"id": "r", "status": "running"}}
    rows = progress(autopilot=snapshot, limit=2)
    assert len(rows) == 2
    assert rows[-1]["final"]
    assert {"id", "run_id", "text", "kind", "at", "final"} <= rows[-1].keys()


def test_actual_nested_activity_does_not_mark_model_completion_final():
    rows = activity_progress({"active": [{"id": "resp-1", "events": [
        {"seq": 4, "ts": 10, "kind": "model_call", "phase": "completed", "title": "Model"},
        {"seq": 5, "ts": 11, "kind": "response_error", "title": "Failed"},
    ]}], "latest": None})
    assert len(rows) == 1
    assert rows[0]["final"] is True


def test_fanout_filters_events_by_run_and_adds_terminal_count():
    rows = fanout_progress({"runs": [{"id": "fan-1", "status": "completed",
                                       "finished_ts": 20,
                                       "results": [{"status": "answered"}, {"status": "failed"}]}],
                            "events": [{"event_id": 1, "run_id": "fan-1", "ts": 10,
                                        "kind": "result", "message": "one done"},
                                       {"event_id": 2, "run_id": "other", "ts": 11,
                                        "kind": "result", "message": "other"}]}, "fan-1")
    assert all("other" not in row["text"] for row in rows)
    assert rows[-1]["final"] and "1 answered, 1 failed" in rows[-1]["text"]


def test_agent_final_summary_uses_recorded_result_and_file_paths():
    rows = activity_progress({"latest": {"id": "r-agent", "status": "complete",
        "result_summary": "Created the report", "files": [{"path": "reports/analysis.md"}],
        "events": [{"kind": "response_complete", "seq": 9, "ts": 20, "summary": "1 tool call"}]}})
    assert "Created the report" in rows[-1]["text"]
    assert "reports/analysis.md" in rows[-1]["text"]
    assert "no validation receipt recorded" in rows[-1]["text"]


def test_fleet_eta_requires_observed_duration_and_known_slots():
    root = {"id": "r", "status": "running", "requested_agents": 3, "worker_slots": 2}
    children = [{"id": "a", "parent_id": "r", "status": "done", "started_ts": 10, "finished_ts": 20},
                {"id": "b", "parent_id": "r", "status": "running", "updated_ts": 30},
                {"id": "c", "parent_id": "r", "status": "queued", "updated_ts": 30}]
    rows = fleet_progress({"agents": [root, *children]})
    assert any("estimate about 10s remaining" in row["text"] for row in rows)
    root["worker_slots"] = 0
    assert all("estimate" not in row["text"] for row in fleet_progress({"agents": [root, *children]}))


def test_activity_local_timestamp_uses_host_timezone_for_cross_source_order():
    from datetime import datetime
    stamp = "2026-10-01T12:15:00"
    rows = activity_progress({"events": [{"kind": "tool_result", "ts": stamp, "title": "finished"}]})
    assert rows[0]["at"] == datetime.fromisoformat(stamp).timestamp()
