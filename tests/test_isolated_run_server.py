import logging

import pytest

import permission_modes
import server
from sonder_runtime.adapters.security.approval_ledger import ApprovalLedger


def _authorize(monkeypatch):
    monkeypatch.setattr(server, "_admin_account_from_token", lambda _token: {"role": "developer"})
    monkeypatch.setattr(server.admin_auth, "require", lambda _account, _role: (True, ""))


def _authorized_args():
    return {
        "token": "developer-token",
        "acknowledge_isolation_limits": True,
    }


def _ledger(monkeypatch, tmp_path):
    store = ApprovalLedger(tmp_path / "approvals.db")
    monkeypatch.setattr(permission_modes, "_approval_ledger", lambda: store)
    return store


def _served(arguments):
    """The surface's view of a call: what the gate digests and the handler reads."""
    return server.approved_call_reach("isolated_run", arguments)


def _writable_call(tmp_path):
    return {
        "image": "busybox", "argv_json": '["true"]', "project": str(tmp_path),
        "writable_workspace": True, **_authorized_args(),
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
        token="developer-token",
    )
    assert "acknowledge_isolation_limits=true" in denied
    assert launched == []


def test_writable_workspace_needs_a_one_shot_approval_of_exactly_this_call(monkeypatch, tmp_path):
    _authorize(monkeypatch)
    store = _ledger(monkeypatch, tmp_path)
    launched = []
    monkeypatch.setattr(server.isolated_runner, "run_isolated", lambda **kwargs: launched.append(kwargs))
    arguments = _writable_call(tmp_path)
    with _served(arguments):
        denied = server.isolated_run(**arguments)
    call = permission_modes.call_id(permission_modes.call_digest("isolated_run", arguments))
    assert "one-shot approval" in denied
    assert "/approve %s" % call in denied
    assert launched == []
    # The refusal is a gate, not a wall: the call is waiting in /approvals.
    pending = store.pending()
    assert [row.tool for row in pending] == ["isolated_run"]
    assert pending[0].call_id == call
    assert "writable_workspace" in pending[0].preview


def test_a_writable_run_outside_any_surface_is_refused(monkeypatch, tmp_path):
    """A direct Python call has no surface view of its arguments, so nothing
    could have approved it; the handler fails closed rather than digesting a
    view of its own that no operator ever saw."""
    _authorize(monkeypatch)
    _ledger(monkeypatch, tmp_path)
    launched = []
    monkeypatch.setattr(server.isolated_runner, "run_isolated", lambda **kwargs: launched.append(kwargs))
    denied = server.isolated_run(**_writable_call(tmp_path))
    assert "only a served call" in denied
    assert launched == []


def _launch_ok(monkeypatch, tmp_path, seen):
    monkeypatch.setattr(
        server.isolated_runner, "run_isolated",
        lambda **kwargs: seen.update(kwargs) or {
            "ok": True, "returncode": 0, "stdout": "", "stderr": "",
            "error": "", "runtime": "docker", "project": str(tmp_path),
            "writable_workspace": True,
        },
    )


def test_writable_workspace_runs_once_per_approval_when_the_mode_let_it_through(monkeypatch, tmp_path):
    """``auto``, an allow rule or an attended yes brings the call to the handler
    without the gate spending anything; the handler spends the operator's
    approval of exactly this call itself, once."""
    _authorize(monkeypatch)
    store = _ledger(monkeypatch, tmp_path)
    seen = {}
    _launch_ok(monkeypatch, tmp_path, seen)
    arguments = _writable_call(tmp_path)
    digest = permission_modes.call_digest("isolated_run", arguments)
    store.issue("isolated_run", digest, approver="nathan", surface="console")

    with _served(arguments):
        output = server.isolated_run(**arguments)
    assert output.startswith("isolated status: ok")
    assert seen["writable_workspace"] is True
    assert store.approvals() == []  # spent

    seen.clear()
    with _served(arguments):
        denied = server.isolated_run(**arguments)
    assert "one-shot approval" in denied
    assert seen == {}


def test_writable_workspace_honours_the_approval_the_gate_spent(monkeypatch, tmp_path):
    """In ``manual`` the gate refuses the unattended call, the operator approves
    it, and the next unchanged call spends the approval at the gate; the
    handler sees that spend and asks for nothing more."""
    _authorize(monkeypatch)
    store = _ledger(monkeypatch, tmp_path)
    seen = {}
    _launch_ok(monkeypatch, tmp_path, seen)
    arguments = _writable_call(tmp_path)
    digest = permission_modes.call_digest("isolated_run", arguments)
    store.issue("isolated_run", digest, approver="nathan", surface="console")

    with _served(arguments):
        decision = permission_modes.decide(
            "isolated_run", mode="manual", interactive=False, surface="mcp",
            arguments=arguments, approval_ledger=store,
        )
        assert decision.allowed and decision.source == "approval"
        output = server.isolated_run(**arguments)
    assert output.startswith("isolated status: ok")
    assert seen["writable_workspace"] is True
    assert store.approvals() == []


def test_a_different_call_does_not_spend_the_approval(monkeypatch, tmp_path):
    _authorize(monkeypatch)
    store = _ledger(monkeypatch, tmp_path)
    launched = []
    monkeypatch.setattr(server.isolated_runner, "run_isolated", lambda **kwargs: launched.append(kwargs))
    approved = _writable_call(tmp_path)
    store.issue(
        "isolated_run", permission_modes.call_digest("isolated_run", approved),
        approver="nathan", surface="console",
    )
    other = {**approved, "argv_json": '["sh", "-c", "rm -rf /workspace"]'}
    with _served(other):
        denied = server.isolated_run(**other)
    assert "one-shot approval" in denied
    assert launched == []
    assert len(store.approvals()) == 1  # still open, for the call it was for


def test_the_retired_isolated_codes_warn_once_and_grant_nothing(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("SONDER_ISOLATED_APPROVAL_CODE", "execute-secret-value")
    monkeypatch.setenv("SONDER_ISOLATED_WRITE_APPROVAL_CODE", "write-secret-value")
    server._RETIRED_ISOLATED_CODE_WARNED.clear()
    launched = []
    monkeypatch.setattr(server.isolated_runner, "run_isolated", lambda **kwargs: launched.append(kwargs))
    with caplog.at_level(logging.WARNING, logger="sonder.server"):
        first = server.isolated_run("busybox", '["true"]', str(tmp_path))
        second = server.isolated_run("busybox", '["true"]', str(tmp_path))
    assert "developer token" in first and "developer token" in second
    assert launched == []
    warnings = [r for r in caplog.records if "no longer honoured" in r.getMessage()]
    assert len(warnings) == 1
    assert "execute-secret-value" not in warnings[0].getMessage()
    assert "write-secret-value" not in warnings[0].getMessage()


def test_every_denial_records_secret_free_direct_tool_audit(monkeypatch, tmp_path):
    records = []
    monkeypatch.setattr(
        server, "_record_direct_tool",
        lambda name, args, **kwargs: records.append((name, args, kwargs)),
    )
    server.isolated_run(
        "busybox", '["true"]', str(tmp_path),
        token="token-secret-value",
    )
    _authorize(monkeypatch)
    _ledger(monkeypatch, tmp_path)
    server.isolated_run(
        "busybox", '["true"]', str(tmp_path),
        token="developer-token",
    )
    arguments = _writable_call(tmp_path)
    with _served(arguments):
        server.isolated_run(**arguments)
    assert [row[1]["denial"] for row in records] == [
        "authorization-denied",
        "risk-acknowledgement-denied",
        "writable-authorization-denied",
    ]
    rendered = repr(records)
    for secret in ("token-secret-value", "developer-token"):
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
