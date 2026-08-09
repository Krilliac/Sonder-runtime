import json
import os
import sys

import pytest

import code_runner
import server
import sonder_config
import sonder_logging
import sonder_serve
import unsafe_lab


ACK = unsafe_lab.ACKNOWLEDGEMENT


@pytest.mark.parametrize(
    "value",
    ["", "1", "true", "yes", ACK.lower(), ACK + " ", ACK[:-1]],
)
def test_truthy_typo_and_whitespace_acknowledgements_fail_closed(value):
    state = unsafe_lab.inspect(
        env={unsafe_lab.ACK_ENV: value, "SONDER_HOST": "127.0.0.1"},
        privilege_probe=lambda: False,
    )
    assert state.requested is True
    assert state.enabled is False
    assert "exactly match" in state.error


def test_missing_acknowledgement_preserves_safe_default():
    state = unsafe_lab.inspect(env={}, privilege_probe=lambda: False)
    assert state == unsafe_lab.State(requested=False, enabled=False)
    assert "safe default" in unsafe_lab.status_line(
        env={}, privilege_probe=lambda: False
    )


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.8", "host.test"])
def test_unsafe_lab_refuses_non_loopback_exposure(host):
    state = unsafe_lab.inspect(
        env={unsafe_lab.ACK_ENV: ACK, "SONDER_HOST": host},
        privilege_probe=lambda: False,
    )
    assert state.enabled is False
    assert "non-loopback" in state.error


def test_unsafe_lab_refuses_privileged_process():
    state = unsafe_lab.inspect(
        env={unsafe_lab.ACK_ENV: ACK, "SONDER_HOST": "127.0.0.1"},
        privilege_probe=lambda: True,
    )
    assert state.enabled is False
    assert "root or elevated" in state.error


def test_exact_unprivileged_loopback_activation_writes_durable_warning(tmp_path):
    audit = tmp_path / "audit" / "unsafe.jsonl"
    env = {
        unsafe_lab.ACK_ENV: ACK,
        unsafe_lab.AUDIT_PATH_ENV: str(audit),
        "SONDER_HOST": "::1",
    }
    unsafe_lab._audited_processes.discard(os.getpid())
    assert unsafe_lab.require_startup(
        env=env, privilege_probe=lambda: False, audit=True
    ) is True
    payload = json.loads(audit.read_text(encoding="utf-8"))
    assert payload["event"] == "unsafe_lab_activated"
    assert payload["host"] == "::1"
    assert "NOT OS ISOLATION" in payload["warning"]


def test_production_config_rejects_inexact_acknowledgement():
    with pytest.raises(sonder_config.ConfigError, match="exactly match"):
        sonder_config.load_config(env={unsafe_lab.ACK_ENV: "true"})


def test_production_config_checks_final_command_line_host_override(monkeypatch):
    monkeypatch.setattr(unsafe_lab, "is_privileged", lambda: False)
    with pytest.raises(sonder_config.ConfigError, match="non-loopback"):
        sonder_config.load_config(
            env={unsafe_lab.ACK_ENV: ACK},
            overrides={"server.host": "0.0.0.0"},
        )


def test_served_unsafe_mode_refuses_remote_even_with_strong_auth(monkeypatch):
    monkeypatch.setenv(unsafe_lab.ACK_ENV, ACK)
    monkeypatch.setattr(unsafe_lab, "is_privileged", lambda: False)
    with pytest.raises(unsafe_lab.UnsafeLabError, match="non-loopback"):
        sonder_serve._validate_bind_security(
            "0.0.0.0", api_key="k" * 32, auth_mode="api-key", auth_secret=""
        )


def test_normal_agent_and_autopilot_policies_are_unchanged(monkeypatch):
    monkeypatch.delenv(unsafe_lab.ACK_ENV, raising=False)
    calls = []
    monkeypatch.setattr(server, "task_create", lambda *a, **k: calls.append((a, k)))

    denied = server._agent_dispatch(
        "task_create", {"title": "must remain denied"}, read_only=True
    )
    assert denied.startswith("ERROR:")
    assert calls == []
    assert server._autopilot_allowed_tools({"policy": "observe"}) == (
        server._AUTOPILOT_OBSERVE_TOOLS
    )
    assert server._autopilot_allowed_tools({"policy": "workspace"}) == (
        server._AUTOPILOT_WORKSPACE_TOOLS
    )
    assert server._file_bypass_allowed() is False


def test_unsafe_mode_removes_agent_and_autopilot_host_tool_restrictions(monkeypatch):
    monkeypatch.setattr(unsafe_lab, "active", lambda: True)
    calls = []

    def fake_workspace_run(**kwargs):
        calls.append(kwargs)
        return "workspace run accepted"

    monkeypatch.setattr(server, "workspace_run", fake_workspace_run)
    output = server._agent_dispatch(
        "workspace_run",
        {"program": "arbitrary-host-native-program", "args_json": []},
        allow_web=False,
        read_only=True,
        repository_extra_roots="host-selected-project",
    )

    assert output == "workspace run accepted"
    assert calls[0]["program"] == "arbitrary-host-native-program"
    assert server._autopilot_allowed_tools({"policy": "observe"}) is None
    assert server._autopilot_tool_policy({"policy": "observe"}) is None
    assert server._file_bypass_allowed() is True


def test_unsafe_child_environment_scrubs_secret_and_control_names(monkeypatch):
    monkeypatch.setattr(unsafe_lab, "active", lambda: True)
    source = {
        "PATH": "safe-path",
        "AWS_SESSION_TOKEN": "cloud-secret",
        "MY_API_KEY": "provider-secret",
        "CUSTOM_CONTROL_GATE": "control-secret",
        "SONDER_HOME": "runtime-control-path",
        "SONDER_FILE_ROOTS": "runtime-control-roots",
        unsafe_lab.ACK_ENV: ACK,
    }
    child = sonder_logging.child_environment(source)
    assert child == {"PATH": "safe-path"}


def test_unsafe_secret_scrub_is_enforced_at_real_code_subprocess(monkeypatch, tmp_path):
    monkeypatch.setattr(unsafe_lab, "active", lambda: True)
    monkeypatch.setenv("UNRELATED_API_KEY", "must-not-cross")
    monkeypatch.setenv("UNRELATED_SESSION_TOKEN", "must-not-cross-either")
    out = code_runner._run_process(
        [
            sys.executable,
            "-c",
            "import os; print(os.getenv('UNRELATED_API_KEY', 'missing')); "
            "print(os.getenv('UNRELATED_SESSION_TOKEN', 'missing'))",
        ],
        str(tmp_path),
        "",
        10,
        "python",
    )
    assert out["ok"] is True
    assert out["stdout"].splitlines() == ["missing", "missing"]
