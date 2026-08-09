import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import code_runner
import grounding
import game_ladder
import node_verifier
import ruff_verifier
import selfmod
import server
import sonder_config
import sonder_logging
import sonder_serve
import unsafe_lab
import verifiers


ACK = unsafe_lab.ACKNOWLEDGEMENT
ROOT = Path(__file__).resolve().parents[1]


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


def test_security_docs_disclose_direct_mcp_and_hosted_boundaries():
    for relative in ("SECURITY.md", "docs/runbooks/unsafe-lab.md"):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "46 direct MCP call paths" in text
        assert "nested-model" in text
        assert "artifact" in text and "process" in text
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


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_unsafe_lab_refuses_cloud_opt_in(value):
    state = unsafe_lab.inspect(
        env={
            unsafe_lab.ACK_ENV: ACK,
            "SONDER_HOST": "127.0.0.1",
            "SONDER_ALLOW_CLOUD": value,
        },
        privilege_probe=lambda: False,
    )
    assert state.enabled is False
    assert "cloud" in state.error


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://models.example:443",
        "http://192.168.1.9:11434",
        "http://127.0.0.1",
        "ftp://127.0.0.1:11434",
        "http://[::1",
        "not an endpoint",
    ],
)
def test_unsafe_lab_refuses_remote_or_malformed_ollama_endpoint(endpoint):
    state = unsafe_lab.inspect(
        env={
            unsafe_lab.ACK_ENV: ACK,
            "SONDER_HOST": "127.0.0.1",
            "OLLAMA_HOST": endpoint,
            "SONDER_ALLOW_REMOTE_OLLAMA": "1",
        },
        privilege_probe=lambda: False,
    )
    assert state.enabled is False
    assert "OLLAMA_HOST" in state.error


@pytest.mark.parametrize(
    "endpoint", ["127.0.0.1:11434", "http://localhost:11434", "http://[::1]:11434"]
)
def test_unsafe_lab_accepts_loopback_ollama_endpoint(endpoint):
    state = unsafe_lab.inspect(
        env={
            unsafe_lab.ACK_ENV: ACK,
            "SONDER_HOST": "127.0.0.1",
            "OLLAMA_HOST": endpoint,
        },
        privilege_probe=lambda: False,
    )
    assert state.enabled is True


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


def test_audit_write_failure_blocks_mcp_startup(monkeypatch, tmp_path):
    from sonder_runtime.__main__ import cmd_mcp

    calls = []
    monkeypatch.setenv(unsafe_lab.ACK_ENV, ACK)
    monkeypatch.setenv("SONDER_HOST", "127.0.0.1")
    monkeypatch.setenv("OLLAMA_HOST", "http://127.0.0.1:11434")
    monkeypatch.setenv(unsafe_lab.AUDIT_PATH_ENV, str(tmp_path))
    monkeypatch.setattr(unsafe_lab, "is_privileged", lambda: False)
    monkeypatch.setattr(server.mcp, "run", lambda: calls.append("mcp"))
    unsafe_lab._audited_processes.discard(os.getpid())

    with pytest.raises(OSError):
        cmd_mcp(object())
    assert calls == []


@pytest.mark.parametrize(
    ("ack", "privileged", "match"),
    [("true", False, "exactly match"), (ACK, True, "root or elevated")],
)
def test_mcp_startup_refuses_invalid_gate_before_adapter(
    monkeypatch, ack, privileged, match
):
    from sonder_runtime.__main__ import cmd_mcp

    calls = []
    monkeypatch.setenv(unsafe_lab.ACK_ENV, ack)
    monkeypatch.setenv("SONDER_HOST", "127.0.0.1")
    monkeypatch.setenv("OLLAMA_HOST", "http://127.0.0.1:11434")
    monkeypatch.setattr(unsafe_lab, "is_privileged", lambda: privileged)
    monkeypatch.setattr(server.mcp, "run", lambda: calls.append("mcp"))

    with pytest.raises(unsafe_lab.UnsafeLabError, match=match):
        cmd_mcp(object())
    assert calls == []


@pytest.mark.parametrize(
    ("environment", "match"),
    [
        ({"SONDER_ALLOW_CLOUD": "1"}, "cloud"),
        (
            {
                "OLLAMA_HOST": "https://models.example:443",
                "SONDER_ALLOW_REMOTE_OLLAMA": "1",
            },
            "loopback OLLAMA_HOST",
        ),
        ({"OLLAMA_HOST": "http://[::1"}, "loopback OLLAMA_HOST"),
    ],
)
def test_mcp_startup_refuses_nonlocal_model_transport_before_adapter(
    monkeypatch, environment, match
):
    from sonder_runtime.__main__ import cmd_mcp

    calls = []
    monkeypatch.setenv(unsafe_lab.ACK_ENV, ACK)
    monkeypatch.setenv("SONDER_HOST", "127.0.0.1")
    monkeypatch.setenv("OLLAMA_HOST", "http://127.0.0.1:11434")
    monkeypatch.delenv("SONDER_ALLOW_CLOUD", raising=False)
    monkeypatch.delenv("SONDER_ALLOW_REMOTE_OLLAMA", raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(unsafe_lab, "is_privileged", lambda: False)
    monkeypatch.setattr(server.mcp, "run", lambda: calls.append("mcp"))

    with pytest.raises(unsafe_lab.UnsafeLabError, match=match):
        cmd_mcp(object())
    assert calls == []


def test_http_startup_refuses_unsafe_elevation_before_listener(monkeypatch):
    listeners = []
    monkeypatch.setenv(unsafe_lab.ACK_ENV, ACK)
    monkeypatch.setenv("SONDER_HOST", "127.0.0.1")
    monkeypatch.setenv("OLLAMA_HOST", "http://127.0.0.1:11434")
    monkeypatch.setattr(unsafe_lab, "is_privileged", lambda: True)
    monkeypatch.setattr(sonder_serve, "HOST", "127.0.0.1")
    monkeypatch.setattr(sonder_serve, "ThreadingHTTPServer", lambda *a: listeners.append(a))
    monkeypatch.setattr(sys, "argv", ["sonder_serve.py"])

    with pytest.raises(unsafe_lab.UnsafeLabError, match="root or elevated"):
        sonder_serve.main()
    assert listeners == []


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


def test_unsafe_hosted_policy_bypasses_only_nested_models(monkeypatch):
    monkeypatch.setattr(unsafe_lab, "active", lambda: True)
    for tool in (
        "file_read", "artifact_risk_inspect", "process_list",
        "process_memory_risk_inspect",
    ):
        error = server._cloud_agent_tool_policy_error(tool, unsafe=True)
        assert "local-only tool" in error
    assert server._cloud_agent_tool_policy_error(
        "offload", unsafe=True,
    ) == ""
    assert "nested model-spawning" in server._cloud_agent_tool_policy_error(
        "offload", unsafe=False,
    )


def test_unsafe_mode_preserves_artifact_and_process_operator_gates(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(unsafe_lab, "active", lambda: True)
    monkeypatch.delenv(server.process_risk_module.OPT_IN_ENV, raising=False)
    assert json.loads(server.process_list())["status"] == "opt_in_required"

    script = tmp_path / "harmless.py"
    script.write_text("print('not launched')\n", encoding="utf-8")
    output = server.script_run(
        str(script), risk_policy="deny-high", extra_roots=str(tmp_path),
    )
    assert "execution denied by effective policy deny-high" in output
    assert "not launched" not in output


def test_unsafe_child_environment_scrubs_secret_and_control_names(monkeypatch):
    monkeypatch.setattr(unsafe_lab, "active", lambda: True)
    source = {
        "PATH": "safe-path",
        "AWS_SESSION_TOKEN": "cloud-secret",
        "AWS_ACCESS_KEY_ID": "cloud-identity",
        "ANTHROPIC_KEY": "provider-secret-key",
        "DATABASE_URL": "postgres://user:password@host/db",
        "AZURE_STORAGE_CONNECTION_STRING": "connection-secret",
        "MY_API_KEY": "provider-secret",
        "CUSTOM_CONTROL_GATE": "control-secret",
        "SONDER_HOME": "runtime-control-path",
        "SONDER_FILE_ROOTS": "runtime-control-roots",
        unsafe_lab.ACK_ENV: ACK,
    }
    child = sonder_logging.child_environment(source)
    assert child == {"PATH": "safe-path"}


def test_child_environment_does_not_mutate_source_or_parent(monkeypatch, tmp_path):
    monkeypatch.setattr(unsafe_lab, "active", lambda: True)
    monkeypatch.setenv("PARENT_SESSION_TOKEN", "parent-value")
    source = dict(os.environ)
    child = sonder_logging.child_environment(source)
    assert source == dict(os.environ)
    assert "PARENT_SESSION_TOKEN" not in child

    result = code_runner._run_process(
        [
            sys.executable,
            "-c",
            "import os; os.environ['PARENT_SESSION_TOKEN']='child-value'",
        ],
        str(tmp_path),
        "",
        10,
        "python",
    )
    assert result["ok"] is True
    assert os.environ["PARENT_SESSION_TOKEN"] == "parent-value"


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


def test_unsafe_secret_scrub_reaches_grounding_generated_python(monkeypatch):
    monkeypatch.setattr(unsafe_lab, "active", lambda: True)
    monkeypatch.setenv("GROUNDING_API_KEY", "must-not-cross")
    result = grounding.run_code_detail(
        "import os; print(os.getenv('GROUNDING_API_KEY', 'missing'))",
        timeout=10,
    )
    assert result["ok"] is True
    assert result["stdout"] == "missing"


def test_unsafe_secret_scrub_reaches_grounding_language_runner(monkeypatch):
    monkeypatch.setattr(unsafe_lab, "active", lambda: True)
    monkeypatch.setenv("LANGUAGE_SESSION_TOKEN", "must-not-cross")
    ok, output = grounding._run_cmd(
        [
            sys.executable,
            "-c",
            "import os; print(os.getenv('LANGUAGE_SESSION_TOKEN', 'missing'))",
        ],
        timeout=10,
    )
    assert ok is True
    assert output == "missing"


def test_all_grounding_subprocess_calls_receive_scrubbed_environment(monkeypatch):
    monkeypatch.setattr(unsafe_lab, "active", lambda: True)
    monkeypatch.setenv("GROUNDING_CONTROL_GATE", "must-not-cross")
    real_run = grounding.subprocess.run
    environments = []

    def capture(*args, **kwargs):
        environments.append(kwargs.get("env"))
        return real_run(*args, **kwargs)

    monkeypatch.setattr(grounding.subprocess, "run", capture)
    assert grounding.run_code_detail("print('ok')", timeout=10)["ok"] is True
    assert grounding.compile_code("value = 1", timeout=10)[0] is True
    assert grounding._run_cmd([sys.executable, "-c", "print('ok')"], 10)[0] is True
    assert len(environments) == 3
    assert all(env is not None for env in environments)
    assert all("GROUNDING_CONTROL_GATE" not in env for env in environments)


def test_campaign_and_selfmod_subprocesses_receive_scrubbed_environment(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(unsafe_lab, "active", lambda: True)
    monkeypatch.setenv("MODEL_AUTHORED_SESSION_TOKEN", "must-not-cross")
    campaign_environments = []

    class Completed:
        returncode = 1
        stdout = ""
        stderr = "candidate test failed"

    def campaign_run(*args, **kwargs):
        campaign_environments.append(kwargs.get("env"))
        return Completed()

    monkeypatch.setattr(subprocess, "run", campaign_run)
    ok, _output, infra = server._repo_repair_pytest(tmp_path, 5)
    assert ok is False and infra == ""
    assert "MODEL_AUTHORED_SESSION_TOKEN" not in campaign_environments[0]

    selfmod_environments = []

    def selfmod_run(*args, **kwargs):
        selfmod_environments.append(kwargs.get("env"))
        return Completed()

    monkeypatch.setattr(selfmod.subprocess, "run", selfmod_run)
    selfmod._run([sys.executable, "-c", "print('test')"], tmp_path)
    selfmod._git(tmp_path, "status", "--short")
    assert len(selfmod_environments) == 2
    assert all(env is not None for env in selfmod_environments)
    assert all(
        "MODEL_AUTHORED_SESSION_TOKEN" not in env
        for env in selfmod_environments
    )


@pytest.mark.parametrize(
    ("module", "invoke"),
    [
        (verifiers, lambda: verifiers._run([sys.executable, "--version"])),
        (node_verifier, lambda: node_verifier._run(["node", "fixture.js"])),
        (ruff_verifier, lambda: ruff_verifier._run(["ruff", "check", "-"], "x=1")),
        (
            game_ladder,
            lambda: game_ladder._ground_capture("print('ok')", "console"),
        ),
    ],
)
def test_every_model_artifact_verifier_scrubs_child_environment(
    monkeypatch, module, invoke,
):
    monkeypatch.setattr(unsafe_lab, "active", lambda: True)
    monkeypatch.setenv("VERIFIER_PRIVATE_KEY", "must-not-cross")
    environments = []

    class Completed:
        returncode = 0
        stdout = b""
        stderr = b""

    def capture(*args, **kwargs):
        environments.append(kwargs.get("env"))
        return Completed()

    monkeypatch.setattr(module.subprocess, "run", capture)
    if module is game_ladder:
        monkeypatch.setattr(game_ladder, "python_interpreter", lambda: sys.executable)
    invoke()
    assert environments
    assert all(env is not None for env in environments)
    assert all("VERIFIER_PRIVATE_KEY" not in env for env in environments)


def test_root_bypass_requires_exact_active_unsafe_gate(monkeypatch, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("outside-root-evidence", encoding="utf-8")
    for name in (
        unsafe_lab.ACK_ENV,
        "SONDER_FILE_BYPASS",
        "SONDER_FILE_APPROVAL_CODE",
        "SONDER_FILE_ROOTS",
        "SONDER_FILE_ROOTS_FILE",
        "SONDER_ALLOW_CLOUD",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SONDER_HOST", "127.0.0.1")
    monkeypatch.setenv("OLLAMA_HOST", "http://127.0.0.1:11434")
    monkeypatch.setattr(unsafe_lab, "is_privileged", lambda: False)

    assert server.file_read(str(outside)).startswith("ERROR:")

    monkeypatch.setenv(unsafe_lab.ACK_ENV, "true")
    malformed = server.file_read(str(outside))
    assert malformed.startswith("ERROR:")
    assert "exactly match" in malformed

    audit = tmp_path / "unsafe-audit.jsonl"
    monkeypatch.setenv(unsafe_lab.ACK_ENV, ACK)
    monkeypatch.setenv(unsafe_lab.AUDIT_PATH_ENV, str(audit))
    unsafe_lab._audited_processes.discard(os.getpid())
    allowed = server.file_read(str(outside))
    assert "outside-root-evidence" in allowed
