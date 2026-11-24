"""Regression tests for local-open permission-mode snapback and mass clear routes."""
from __future__ import annotations

import time
import pytest

import permission_modes
from sonder_runtime.adapters.persistence import agent_lanes as lane_store_mod
from sonder_runtime.adapters.persistence import autopilot_store, fleet_store
from sonder_runtime.interfaces.http.agent_work_routes import handle_agent_work_request
from sonder_runtime.interfaces.http.chat_tool_stream import handle_permission_mode_post


def test_local_open_set_mode_without_escalation_refuses_auto_when_persisted_auto(monkeypatch, tmp_path):
    """When disk/global mode is auto, a capped local-open caller sees manual and
    must get ModeEscalationRefused when posting auto instead of a fake 200."""
    monkeypatch.setattr(permission_modes, "_state_path", lambda: str(tmp_path / "permission_mode.json"))
    permission_modes._reset_for_tests()
    permission_modes.set_global_mode(permission_modes.AUTO)
    assert permission_modes.current_mode() == permission_modes.AUTO

    with permission_modes.mode_scope("local-open", "", local_open=True):
        assert permission_modes.current_mode() == permission_modes.MANUAL
        with pytest.raises(permission_modes.ModeEscalationRefused) as exc_info:
            permission_modes.set_mode_without_escalation(permission_modes.AUTO)
        assert exc_info.value.current == permission_modes.MANUAL
        assert exc_info.value.target == permission_modes.AUTO

        # Lowering to manual or plan remains permitted under local-open.
        assert permission_modes.set_mode_without_escalation(permission_modes.MANUAL) == permission_modes.MANUAL
        assert permission_modes.current_mode() == permission_modes.MANUAL


def test_handle_permission_mode_post_refuses_capped_local_open_auto(monkeypatch, tmp_path):
    monkeypatch.setattr(permission_modes, "_state_path", lambda: str(tmp_path / "permission_mode.json"))
    permission_modes._reset_for_tests()
    permission_modes.set_global_mode(permission_modes.AUTO)

    sent = []

    class FakeHandler:
        headers = {}

        def _correlation(self):
            return "corr-test"

        def _send_json_payload(self, body, status=200):
            sent.append((status, body))

    ctx = {"mode": "local-open", "authorized": True, "api_key": False, "account": None}
    handle_permission_mode_post(
        FakeHandler(),
        {"mode": "auto"},
        ctx,
        permission_policy=permission_modes,
        principal_of=lambda _: "local-open",
        idempotent_action=lambda _c, _k, _a, fn: fn(),
        send_idempotency_refusal=lambda _h, _r: False,
        observe_state=lambda: None,
        mode_data=lambda: {"mode": permission_modes.current_mode(), "label": permission_modes.MODE_LABELS[permission_modes.current_mode()]},
        approver_of=lambda _: "local-open",
    )
    assert len(sent) == 1
    status, body = sent[0]
    assert status == 403
    assert body["error"]["code"] == "PERMISSION_MODE_ESCALATION_REFUSED"
    assert body["mode"] == "manual"


def test_autopilot_and_fleet_clear_finished_preserves_active(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_HOME", str(tmp_path))
    autopilot_store.reset_schema_cache_for_tests()
    fleet_store.reset_schema_cache_for_tests()
    autopilot_store.clear_all()
    fleet_store.clear_all()

    now = time.time()
    with autopilot_store._write_transaction() as conn:
        for run_id, owner, status in [
            ("ap-done", "owner-a", "completed"),
            ("ap-fail", "owner-a", "failed"),
            ("ap-run", "owner-a", "executing"),
            ("ap-other", "owner-b", "completed"),
        ]:
            conn.execute(
                """INSERT INTO autopilot_runs
                   (id, objective, project, request_owner, tier, policy, status, phase, created_ts, updated_ts)
                   VALUES (?, ?, '', ?, 'code', 'safe', ?, 'exec', ?, ?)""",
                (run_id, f"obj {run_id}", owner, status, now, now),
            )

    cleared_ap = autopilot_store.clear_finished(request_owner="owner-a")
    assert cleared_ap == 2
    with autopilot_store._connect() as conn:
        remaining = {r[0] for r in conn.execute("SELECT id FROM autopilot_runs").fetchall()}
    assert remaining == {"ap-run", "ap-other"}

    with fleet_store._write_transaction() as conn:
        for aid, status in [
            ("fl-done", "completed"),
            ("fl-fail", "failed"),
            ("fl-run", "running"),
        ]:
            conn.execute(
                """INSERT INTO fleet_agents
                   (id, owner_id, owner_pid, role, parent_id, task, status, started_ts, updated_ts)
                   VALUES (?, 'o1', 1, 'master', '', 'task', ?, ?, ?)""",
                (aid, status, now, now),
            )
    cleared_fl = fleet_store.clear_finished()
    assert cleared_fl == 2
    with fleet_store._connect() as conn:
        remaining_fl = {r[0] for r in conn.execute("SELECT id FROM fleet_agents").fetchall()}
    assert remaining_fl == {"fl-run"}


def test_agent_lanes_clear_finished_and_http_route(tmp_path):
    db = tmp_path / "lanes.db"
    store = lane_store_mod.SQLiteAgentLaneStore(db, sessions=None)
    with store.transaction() as tx:
        for lid, principal, status in [
            ("lane-done", "p1", "completed"),
            ("lane-fail", "p1", "failed"),
            ("lane-run", "p1", "running"),
            ("lane-other", "p2", "completed"),
        ]:
            tx.conn.execute(
                "INSERT INTO agent_lanes (id, principal, parent_session, data) VALUES (?, ?, 'ps1', ?)",
                (lid, principal, lane_store_mod.encode({"id": lid, "principal": principal, "status": status})),
            )
        cleared = tx.clear_finished("p1")
        assert cleared == 2
        left = {r[0] for r in tx.conn.execute("SELECT id FROM agent_lanes").fetchall()}
        assert left == {"lane-run", "lane-other"}

    # Verify HTTP handler dispatches POST /v1/agent-lanes/clear & /v1/background-work/clear
    sent = []

    class FakeHandler:
        path = "/v1/agent-lanes/clear"
        headers = {}

        def _request_auth_context(self):
            return {"authorized": True, "mode": "local-open"}

        def _correlation(self):
            return "c1"

        def _send_json_payload(self, body, status=200):
            sent.append((status, body))

    class FakeLaneSvc:
        def clear_finished(self, context, *, lane_ids=None):
            return {"cleared_lanes": 3}

    class FakeApp:
        def agent_lanes(self):
            return FakeLaneSvc()

    class FakeCtx:
        principal_id = "p1"
        workspace_roots = ()

    class FakePolicy:
        def forget_spent_approval(self):
            pass

    handled = handle_agent_work_request(
        FakeHandler(),
        "POST",
        "/v1/agent-lanes/clear",
        {},
        application_of=FakeApp,
        context_of=lambda _a, _c: FakeCtx(),
        background_of=lambda _app, _a: None,
        policy=FakePolicy(),
        logger=None,
    )
    assert handled is True
    assert sent[-1] == (200, {"cleared_lanes": 3})

    handled_bg = handle_agent_work_request(
        FakeHandler(),
        "POST",
        "/v1/background-work/clear",
        {},
        application_of=FakeApp,
        context_of=lambda _a, _c: FakeCtx(),
        background_of=lambda _app, _a: None,
        policy=FakePolicy(),
        logger=None,
        clear_background_of=lambda _auth, project="": {"cleared_autopilot": 1, "cleared_fleets": 2},
    )
    assert handled_bg is True
    assert sent[-1] == (200, {"cleared_autopilot": 1, "cleared_fleets": 2})
