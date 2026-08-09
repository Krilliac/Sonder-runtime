import server


def test_isolated_run_is_direct_mcp_only_and_defaults_to_ask(tmp_path):
    assert "isolated_run" in server.tool_manifest()
    assert "isolated_run" not in server._agent_tool_help()
    assert "isolated_run" not in server._AUTOPILOT_OBSERVE_TOOLS
    assert "isolated_run" not in server._AUTOPILOT_WORKSPACE_TOOLS
    policy = server.permission_rules.check(str(tmp_path), "isolated_run")
    assert policy["action"] == "ask"


def test_server_forwards_only_the_fixed_isolated_contract(monkeypatch, tmp_path):
    seen = {}
    def fake_run(**kwargs):
        seen.update(kwargs)
        return {
            "ok": True, "returncode": 0, "stdout": "safe\n", "stderr": "",
            "error": "", "runtime": "docker", "project": str(tmp_path),
            "writable_workspace": False,
        }
    monkeypatch.setattr(server.isolated_runner, "run_isolated", fake_run)
    output = server.isolated_run("busybox:1.36", '["true"]', str(tmp_path), timeout=7)
    assert output.startswith("isolated status: ok")
    assert seen == {
        "image": "busybox:1.36", "argv_json": '["true"]', "project": str(tmp_path),
        "stdin": "", "writable_workspace": False, "timeout": 7,
        "memory_mb": 512, "cpus": 1.0, "pids": 64, "output_bytes": 131072,
    }


def test_server_rejects_bad_request_without_launch(monkeypatch, tmp_path):
    monkeypatch.setattr(
        server.isolated_runner, "run_isolated",
        lambda **_kwargs: (_ for _ in ()).throw(ValueError("bad argv")),
    )
    assert server.isolated_run("busybox", "[]", str(tmp_path)) == "ERROR: bad argv"
