import server


def _authorize(monkeypatch):
    monkeypatch.setenv("SONDER_ISOLATED_APPROVAL_CODE", "execute-secret")
    monkeypatch.setenv("SONDER_ISOLATED_WRITE_APPROVAL_CODE", "write-secret")
    monkeypatch.setattr(server, "_admin_account_from_token", lambda _token: {"role": "developer"})
    monkeypatch.setattr(server.admin_auth, "require", lambda _account, _role: (True, ""))


def _authorized_args():
    return {
        "token": "developer-token",
        "approval": "execute-secret",
        "acknowledge_isolation_limits": True,
    }


def test_isolated_run_is_direct_mcp_only_and_defaults_to_ask(tmp_path):
    assert "isolated_run" in server.tool_manifest()
    assert "isolated_run" not in server._agent_tool_help()
    assert "isolated_run" not in server._AUTOPILOT_OBSERVE_TOOLS
    assert "isolated_run" not in server._AUTOPILOT_WORKSPACE_TOOLS
    policy = server.permission_rules.check(str(tmp_path), "isolated_run")
    assert policy["action"] == "ask"


def test_server_forwards_only_the_fixed_isolated_contract(monkeypatch, tmp_path):
    _authorize(monkeypatch)
    seen = {}
    def fake_run(**kwargs):
        seen.update(kwargs)
        return {
            "ok": True, "returncode": 0, "stdout": "safe\n", "stderr": "",
            "error": "", "runtime": "docker", "project": str(tmp_path),
            "writable_workspace": False,
        }
    monkeypatch.setattr(server.isolated_runner, "run_isolated", fake_run)
    output = server.isolated_run(
        "busybox:1.36", '["true"]', str(tmp_path), timeout=7,
        **_authorized_args(),
    )
    assert output.startswith("isolated status: ok")
    assert seen == {
        "image": "busybox:1.36", "argv_json": '["true"]', "project": str(tmp_path),
        "stdin": "", "writable_workspace": False, "timeout": 7,
        "memory_mb": 512, "cpus": 1.0, "pids": 64, "output_bytes": 131072,
    }


def test_server_rejects_bad_request_without_launch(monkeypatch, tmp_path):
    _authorize(monkeypatch)
    records = []
    monkeypatch.setattr(
        server, "_record_direct_tool",
        lambda name, args, **kwargs: records.append((name, args, kwargs)),
    )
    monkeypatch.setattr(
        server.isolated_runner, "run_isolated",
        lambda **_kwargs: (_ for _ in ()).throw(ValueError("bad argv")),
    )
    assert server.isolated_run(
        "secret-image-name", "[]", str(tmp_path / "secret-project-name"),
        **_authorized_args()
    ) == "ERROR: bad argv"
    assert records[0][1] == {
        "failure": "policy-or-runtime-error", "writable_workspace": False,
    }
    assert "secret-image-name" not in repr(records)
    assert "secret-project-name" not in repr(records)


def test_direct_mcp_call_requires_developer_approval_and_ack(monkeypatch, tmp_path):
    launched = []
    monkeypatch.setattr(server.isolated_runner, "run_isolated", lambda **kwargs: launched.append(kwargs))
    denied = server.isolated_run("busybox", '["true"]', str(tmp_path))
    assert "developer token" in denied
    _authorize(monkeypatch)
    denied = server.isolated_run(
        "busybox", '["true"]', str(tmp_path),
        token="developer-token", approval="execute-secret",
    )
    assert "acknowledge_isolation_limits=true" in denied
    assert launched == []


def test_writable_workspace_requires_separate_host_secret(monkeypatch, tmp_path):
    _authorize(monkeypatch)
    launched = []
    monkeypatch.setattr(server.isolated_runner, "run_isolated", lambda **kwargs: launched.append(kwargs))
    denied = server.isolated_run(
        "busybox", '["true"]', str(tmp_path), writable_workspace=True,
        **_authorized_args(),
    )
    assert "separate host" in denied
    assert launched == []


def test_writable_workspace_accepts_only_separate_matching_secret(monkeypatch, tmp_path):
    _authorize(monkeypatch)
    seen = {}
    monkeypatch.setattr(
        server.isolated_runner, "run_isolated",
        lambda **kwargs: seen.update(kwargs) or {
            "ok": True, "returncode": 0, "stdout": "", "stderr": "",
            "error": "", "runtime": "docker", "project": str(tmp_path),
            "writable_workspace": True,
        },
    )
    output = server.isolated_run(
        "busybox", '["true"]', str(tmp_path), writable_workspace=True,
        write_approval="write-secret", **_authorized_args(),
    )
    assert output.startswith("isolated status: ok")
    assert seen["writable_workspace"] is True


def test_every_denial_records_secret_free_direct_tool_audit(monkeypatch, tmp_path):
    records = []
    monkeypatch.setattr(
        server, "_record_direct_tool",
        lambda name, args, **kwargs: records.append((name, args, kwargs)),
    )
    server.isolated_run(
        "busybox", '["true"]', str(tmp_path),
        token="token-secret-value", approval="execute-secret-value",
    )
    _authorize(monkeypatch)
    server.isolated_run(
        "busybox", '["true"]', str(tmp_path),
        token="developer-token", approval="execute-secret",
    )
    server.isolated_run(
        "busybox", '["true"]', str(tmp_path), writable_workspace=True,
        token="developer-token", approval="execute-secret",
        acknowledge_isolation_limits=True,
        write_approval="wrong-write-secret-value",
    )
    assert [row[1]["denial"] for row in records] == [
        "authorization-denied",
        "risk-acknowledgement-denied",
        "writable-authorization-denied",
    ]
    rendered = repr(records)
    for secret in (
        "token-secret-value", "execute-secret-value", "developer-token",
        "execute-secret", "wrong-write-secret-value", "write-secret",
    ):
        assert secret not in rendered
