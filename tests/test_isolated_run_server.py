import pytest

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


@pytest.mark.parametrize("field", ["timeout", "memory_mb", "cpus", "pids", "output_bytes"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_direct_mcp_rejects_and_audits_nonfinite_resource_limits(
    monkeypatch, tmp_path, field, value
):
    _authorize(monkeypatch)
    records = []
    monkeypatch.setattr(
        server, "_record_direct_tool",
        lambda name, args, **kwargs: records.append((name, args, kwargs)),
    )
    monkeypatch.setattr(
        server.isolated_runner, "detect_runtime",
        lambda: (
            "docker", str(tmp_path / "docker.exe"),
            ("--host", "npipe:////./pipe/docker_engine"),
        ),
    )
    monkeypatch.setattr(
        server.isolated_runner, "_inspect_image_policy",
        lambda *_args: "sha256:" + "a" * 64,
    )
    monkeypatch.setattr(
        server.isolated_runner, "resolve_project", lambda project: project
    )
    monkeypatch.setattr(
        server.isolated_runner, "_run_bounded",
        lambda *_args: pytest.fail("invalid resource input reached process launch"),
    )
    kwargs = _authorized_args()
    kwargs[field] = value
    output = server.isolated_run("busybox", '["true"]', str(tmp_path), **kwargs)
    assert output.startswith("ERROR:")
    assert len(records) == 1
    assert records[0][0] == "isolated_run"
    assert records[0][1] == {
        "failure": "policy-or-runtime-error", "writable_workspace": False,
    }
    assert records[0][2]["summary"] == "isolated runner rejected request"
    assert field not in repr(records)


@pytest.mark.parametrize(
    "requested,expected",
    [
        (10 ** 100, (120, 4096, 4.0, 256, 262144)),
        (-(10 ** 100), (1, 64, 0.1, 16, 1024)),
    ],
)
def test_direct_mcp_clamps_finite_resource_boundaries(
    monkeypatch, tmp_path, requested, expected
):
    _authorize(monkeypatch)
    captured = {}
    monkeypatch.setattr(
        server.isolated_runner, "detect_runtime",
        lambda: (
            "docker", str(tmp_path / "docker.exe"),
            ("--host", "npipe:////./pipe/docker_engine"),
        ),
    )
    monkeypatch.setattr(
        server.isolated_runner, "_inspect_image_policy",
        lambda *_args: "sha256:" + "a" * 64,
    )
    monkeypatch.setattr(
        server.isolated_runner, "resolve_project", lambda project: project
    )
    monkeypatch.setattr(
        server.isolated_runner, "_project_identity", lambda _project: ()
    )
    def fake_run(argv, _runtime_name, _runtime_path, _runtime_prefix, _name,
                 _stdin, timeout, output_limit, *_rest):
        captured.update(argv=argv, timeout=timeout, output_limit=output_limit)
        return {
            "ok": True, "returncode": 0, "stdout": "", "stderr": "",
            "error": "", "cleanup": "not-required",
        }
    monkeypatch.setattr(server.isolated_runner, "_run_bounded", fake_run)
    output = server.isolated_run(
        "busybox", '["true"]', str(tmp_path), timeout=requested,
        memory_mb=requested, cpus=requested, pids=requested,
        output_bytes=requested, **_authorized_args(),
    )
    timeout, memory, cpus, pids, output_limit = expected
    assert output.startswith("isolated status: ok")
    assert captured["timeout"] == timeout
    assert captured["output_limit"] == output_limit
    assert "--memory=%dm" % memory in captured["argv"]
    assert "--cpus=%g" % cpus in captured["argv"]
    assert "--pids-limit=%d" % pids in captured["argv"]
