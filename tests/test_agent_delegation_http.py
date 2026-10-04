"""Real HTTP routing and durable lane receipts with no model or network provider."""
from types import SimpleNamespace

import pytest

from tests.test_agent_lane_http_wiring import lane_http as lane_http, auth, request
from tests.test_delegate_runtime import _service
from sonder_runtime.interfaces.http import serve
from sonder_runtime.interfaces.orchestration_commands import reply_receipt_fields


@pytest.fixture
def delegate_app(tmp_path, monkeypatch):
    import permission_modes
    lanes, _ = _service(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    application = SimpleNamespace(agent_lanes=lambda: lanes,
        config=SimpleNamespace(state=SimpleNamespace(home=str(tmp_path / "state"), workspace_roots=(project,))))
    monkeypatch.setattr("sonder_runtime.bootstrap.app.default_app", lambda: application)
    monkeypatch.setattr(permission_modes, "_LOADED", True)
    monkeypatch.setitem(permission_modes._STATE, "mode", permission_modes.AUTO)
    monkeypatch.setattr(permission_modes, "_rule_lookup", lambda name: None)
    return lanes, project


@pytest.mark.parametrize("selected", [False, True])
def test_http_delegate_replay_list_detail_and_cancel(lane_http, delegate_app, monkeypatch, tmp_path, selected):
    lanes, project = delegate_app
    auth(monkeypatch)
    payload = {"task": "write primes.py", "command_id": "http-delegate-1",
               "parent_session_id": "chat-one", "project": str(project) if selected else ""}
    status, first = request(lane_http, "/v1/agent-lanes/delegate", payload)
    assert status == 202, first
    status, repeated = request(lane_http, "/v1/agent-lanes/delegate", payload)
    assert status == 202 and repeated == first
    lane_id = first["lane"]["id"]
    expected = project if selected else tmp_path / "state" / "creations" / lane_id
    assert first["delegation"]["folder"] == str(expected)
    assert first["lane"]["parent_session_id"] != "chat-one"
    status, listing = request(lane_http, "/v1/agent-lanes?order=newest")
    assert status == 200 and [row["id"] for row in listing["lanes"]] == [lane_id]
    status, detail = request(lane_http, f"/v1/agent-lanes/{lane_id}")
    assert status == 200 and detail["lane"]["task"] == "write primes.py"
    status, cancelled = request(lane_http, f"/v1/agent-lanes/{lane_id}/cancel", {"command_id": "cancel-1"})
    assert status == 202 and cancelled["lane"]["status"] in {"cancel_requested", "cancelled"}
    assert len(lanes.list(serve._http_debug_context({"authorized": True, "mode": "local-open"}, "test"))["lanes"]) == 1


def test_http_delegate_denies_nonadmin_before_creating_folder(lane_http, delegate_app, monkeypatch, tmp_path):
    auth(monkeypatch, account={"username": "reader", "role": "user"})
    status, body = request(lane_http, "/v1/agent-lanes/delegate", {"task": "write primes.py"})
    assert status == 403, body
    assert not (tmp_path / "state").exists()


def test_http_delegate_unauthenticated_never_builds_application(lane_http, monkeypatch):
    auth(monkeypatch, authorized=False)
    monkeypatch.setattr("sonder_runtime.bootstrap.app.default_app", lambda: pytest.fail("auth gate bypassed"))
    assert request(lane_http, "/v1/agent-lanes/delegate", {"task": "write file"})[0] == 401


def test_delegate_slash_returns_trusted_navigation_and_uses_raw_project(delegate_app, monkeypatch):
    _, project = delegate_app
    context = {"authorized": True, "mode": "local-open"}
    reply = serve._handle_slash("/delegate write primes.py", context=context,
        project="storage-id-is-not-a-filesystem-root", workspace_project=str(project),
        lane_session="chat-one", idempotency_key="chat-command-1")
    receipt = reply_receipt_fields(reply)["agent_lane"]
    assert receipt["folder"] == str(project)
    assert receipt["lane_id"] in reply and "\n" not in reply
    assert receipt["open"] == {"surface": "agents", "lane_id": receipt["lane_id"]}


def test_native_delegate_uses_configured_lane_service(delegate_app, monkeypatch):
    import server
    lanes, project = delegate_app
    from sonder_runtime.bootstrap.app import default_app
    application = default_app()
    monkeypatch.setattr(server, "_application", lambda: application)
    reply = server.control_command("/delegate write primes.py", project=str(project), session="native-chat")
    receipt = reply_receipt_fields(reply)["agent_lane"]
    assert receipt["folder"] == str(project)
    _, context = server._agent_lane_context()
    assert lanes.inspect(receipt["lane_id"], context)["lane"]["status"] == "queued"


def test_background_work_http_is_read_only_and_includes_delegation(lane_http, delegate_app, monkeypatch):
    auth(monkeypatch)
    status, receipt = request(lane_http, "/v1/agent-lanes/delegate", {"task": "new lane"})
    assert status == 202, receipt
    status, snapshot = request(lane_http, "/v1/background-work")
    assert status == 200, snapshot
    assert set(snapshot["groups"]) == {"lanes", "fleets", "autopilot"}
    assert snapshot["groups"]["lanes"][0]["id"] == receipt["lane"]["id"]
    assert request(lane_http, "/v1/background-work", {})[0] == 405
