import json

import server


def test_direct_tool_records_deterministic_success(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(
        server.local_probe,
        "probe",
        lambda *args, **kwargs: {
            "status": 204,
            "latency_ms": 3,
            "content_type": "",
            "body_preview": "",
        },
    )
    monkeypatch.setattr(
        server,
        "_record_direct_tool",
        lambda name, args, **kwargs: calls.append((name, args, kwargs)),
    )

    output = server.local_service_probe(
        "http://127.0.0.1:8080/health", method="HEAD", timeout=1.5,
    )

    assert json.loads(output)["status"] == 204
    assert calls[0][0] == "local_service_probe"
    assert calls[0][1] == {
        "url": "http://127.0.0.1:8080/health",
        "method": "HEAD",
        "timeout": 1.5,
    }
    assert calls[0][2]["ok"] is True
    assert calls[0][2]["output"] == output


def test_agent_and_loop_dispatch_route_probe_without_public_web_gate(monkeypatch):
    calls = []
    monkeypatch.setattr(
        server,
        "local_service_probe",
        lambda url, method="GET", timeout=2.0: calls.append(
            (url, method, timeout)
        ) or '{"status": 200}',
    )
    args = {
        "url": "http://localhost:9000/ready",
        "method": "HEAD",
        "timeout": 1.0,
    }
    assert server._agent_dispatch(
        "local_service_probe", args, allow_web=False, read_only=True,
    ) == '{"status": 200}'
    result = server._loop_dispatch({"type": "local_service_probe", **args})
    assert result["ok"] is True
    assert calls == [
        (args["url"], "HEAD", 1.0),
        (args["url"], "HEAD", 1.0),
    ]


def test_probe_is_registered_as_read_only_deduplicated_autopilot_observation():
    assert "local_service_probe" in server.REPOSITORY_READ_ONLY_TOOLS
    assert "local_service_probe" in server.REPOSITORY_AGENT_TOOL_HELP
    assert "local_service_probe" in server.AGENT_TOOL_HELP
    assert "local_service_probe" in server._WORK_INSPECTION_TOOLS
    assert "local_service_probe" in server._AGENT_DEDUPLICATED_INSPECTION_TOOLS
    assert "local_service_probe" in server._AUTOPILOT_OBSERVE_TOOLS
    assert "local_service_probe" not in server._WORK_MUTATION_TOOLS
    assert server._repository_read_only_error(
        "local_service_probe",
        {"url": "http://127.0.0.1:8080/health", "method": "GET"},
    ) == ""
    assert server._autopilot_tool_policy({"policy": "observe"})(
        "local_service_probe",
        {"url": "http://127.0.0.1:8080/health"},
    ) == ""


def test_manifest_and_activity_command_expose_local_only_contract():
    manifest = server.tool_manifest()
    assert "local_service_probe" in manifest
    assert "resolving exclusively to loopback" in manifest
    assert server._agent_activity_command(
        "local_service_probe",
        {"url": "http://127.0.0.1:8080/health", "method": "head"},
    ) == "HEAD http://127.0.0.1:8080/health"
