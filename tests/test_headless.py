import io
import subprocess

import sonder_headless as H


class GateInput:
    def __init__(self, value):
        self.buffer = io.BytesIO(value)


def test_pid_file_paths_use_run_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(H, "run_dir", lambda: tmp_path)

    assert H.pid_file("x") == tmp_path / "x.pid"
    assert H.log_file("x") == tmp_path / "x.log"


def test_start_sonder_skips_when_port_open(monkeypatch):
    monkeypatch.setattr(H, "port_open", lambda host, port: True)

    out = H.start_sonder("127.0.0.1", 11435)

    assert "already listening" in out


def test_start_ollama_skips_when_already_reachable(monkeypatch):
    monkeypatch.setattr(H, "ollama_ok", lambda: True)

    assert H.start_ollama() == "ollama: already reachable"


def test_start_ollama_reports_missing_binary(monkeypatch):
    monkeypatch.setattr(H, "ollama_ok", lambda: False)
    monkeypatch.setattr(H.shutil, "which", lambda exe: None)

    assert H.start_ollama() == "ollama: not installed or not on PATH"


def test_ollama_probe_uses_canonical_client_environment(monkeypatch):
    seen = []
    monkeypatch.setenv("OLLAMA_HOST", "0.0.0.0:11434")

    def fake_open(request, timeout=0):
        seen.append((request.full_url, timeout))
        return io.BytesIO(b'{"models": []}')

    monkeypatch.setattr(H.ollama_endpoint, "open_url", fake_open)

    assert H.ollama_ok() is True
    assert seen == [("http://127.0.0.1:11434/api/tags", 8)]


def test_unavailable_remote_ollama_never_starts_local_daemon(monkeypatch):
    started = []
    monkeypatch.setenv("OLLAMA_HOST", "http://models.example.test:11434")
    monkeypatch.setenv("SONDER_ALLOW_REMOTE_OLLAMA", "1")
    monkeypatch.setattr(H, "ollama_ok", lambda: False)
    monkeypatch.setattr(H, "ollama_exe", lambda: "ollama-test")
    monkeypatch.setattr(H.engine_bundle, "discover_engine_bundle", lambda root: None)
    monkeypatch.setattr(H, "_popen", lambda *args, **kwargs: started.append(1))

    result = H.start_ollama()

    assert "remote endpoint is unavailable" in result
    assert started == []


def test_bind_all_serve_keeps_server_env_while_probes_use_loopback(monkeypatch):
    started = []
    monkeypatch.setenv("OLLAMA_HOST", "0.0.0.0:11434")
    monkeypatch.setattr(H, "ollama_ok", lambda: False)
    monkeypatch.setattr(H, "ollama_exe", lambda: "ollama-test")
    monkeypatch.setattr(H.engine_bundle, "discover_engine_bundle", lambda root: None)
    monkeypatch.setattr(H, "wait_until", lambda fn, seconds: True)
    monkeypatch.setattr(
        H,
        "_popen",
        lambda command, name, env=None: started.append(env) or 123,
    )

    assert "started pid=123" in H.start_ollama()
    assert started[0]["OLLAMA_HOST"] == "0.0.0.0:11434"


def test_python_exe_ignores_broken_venv(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    venv = root / "venv" / "Scripts"
    venv.mkdir(parents=True)
    (venv / "python.exe").write_text("broken", encoding="utf-8")
    monkeypatch.setattr(H, "repo_root", lambda: root)
    monkeypatch.setattr(H, "_python_works", lambda path: False)
    monkeypatch.setattr(H.sys, "executable", "C:/Python/python.exe")

    assert H.python_exe() == "C:/Python/python.exe"


def test_runtime_executables_honor_explicit_bundle_environment(monkeypatch):
    monkeypatch.setenv("SONDER_PYTHON", "C:/bundle/python.exe")
    monkeypatch.setenv("SONDER_OLLAMA_EXE", "C:/bundle/ollama.exe")
    monkeypatch.setattr(H, "_python_works", lambda path: path == "C:/bundle/python.exe")
    assert H.python_exe() == "C:/bundle/python.exe"
    assert H.ollama_exe() == "C:/bundle/ollama.exe"


def test_alias_probe_skips_bootstrap_when_ready(monkeypatch):
    calls = []
    monkeypatch.setattr(H, "ollama_models", lambda: {"sonder:latest"})
    monkeypatch.setattr(H, "ollama_exe", lambda: calls.append("cli") or "")
    ok, message = H.ensure_sonder_alias()
    assert ok is True
    assert "ready" in message
    assert calls == []


def test_remote_http_endpoint_without_local_cli_can_pass_engine_gate(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "https://models.example.test:11434")
    monkeypatch.setenv("SONDER_ALLOW_REMOTE_OLLAMA", "1")
    monkeypatch.setattr(H, "ollama_exe", lambda: "")
    monkeypatch.setattr(
        H.ollama_endpoint,
        "open_url",
        lambda request, timeout=0: io.BytesIO(
            b'{"models": [{"name": "sonder:latest"}]}'
        ),
    )

    assert H.ollama_ok() is True
    assert H.ensure_sonder_alias() == (True, "engine: sonder alias is ready")


def test_reachable_endpoint_without_cli_reports_missing_alias(monkeypatch):
    monkeypatch.setattr(H, "ollama_models", lambda: {"qwen:latest"})
    monkeypatch.setattr(H, "ollama_exe", lambda: "")

    ok, message = H.ensure_sonder_alias()

    assert ok is False
    assert "reachable" in message
    assert "alias is missing" in message


def test_non_latest_sonder_tag_does_not_satisfy_stable_alias(monkeypatch):
    monkeypatch.setattr(H, "ollama_models", lambda: {"sonder:experimental"})
    monkeypatch.setattr(H, "ollama_exe", lambda: "")

    ok, message = H.ensure_sonder_alias()

    assert ok is False
    assert "alias is missing" in message


def test_stop_pid_reports_missing_pid(monkeypatch, tmp_path):
    monkeypatch.setattr(H, "run_dir", lambda: tmp_path)

    assert H.stop_pid("missing") == "missing: no pid file"


def test_stop_pid_uses_taskkill_on_windows(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(H, "run_dir", lambda: tmp_path)
    monkeypatch.setattr(H, "pid_alive", lambda pid: True)
    monkeypatch.setattr(H.os, "name", "nt", raising=False)
    H.pid_file("svc").write_text("123", encoding="ascii")

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(H.subprocess, "run", fake_run)

    out = H.stop_pid("svc")

    assert "stopped pid=123" in out
    assert seen["cmd"][:2] == ["taskkill", "/PID"]


def test_listener_pid_parses_windows_netstat(monkeypatch):
    output = """
  Proto  Local Address          Foreign Address        State           PID
  TCP    127.0.0.1:11435        0.0.0.0:0              LISTENING       4567
"""
    monkeypatch.setattr(H.os, "name", "nt", raising=False)
    monkeypatch.setattr(
        H.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, output, ""),
    )

    assert H._listener_pid("127.0.0.1", 11435) == 4567


def test_managed_pid_repairs_stale_venv_launcher_pid(monkeypatch, tmp_path):
    monkeypatch.setattr(H, "run_dir", lambda: tmp_path)
    H.pid_file("sonder_serve").write_text("111", encoding="ascii")
    monkeypatch.setattr(H, "pid_alive", lambda pid: pid == 222)
    monkeypatch.setattr(H, "port_open", lambda host, port: True)
    monkeypatch.setattr(H, "_listener_pid", lambda host, port: 222)
    monkeypatch.setattr(H, "_is_sonder_server_pid", lambda pid: pid == 222)

    assert H._managed_pid("sonder_serve") == 222
    assert H.pid_file("sonder_serve").read_text(encoding="ascii") == "222"


def test_start_sonder_records_real_listener_pid(monkeypatch, tmp_path):
    monkeypatch.setattr(H, "run_dir", lambda: tmp_path)
    monkeypatch.setattr(H, "port_open", lambda host, port: False)
    monkeypatch.setattr(H, "wait_until", lambda fn, seconds: True)
    monkeypatch.setattr(H, "python_exe", lambda: "python")
    monkeypatch.setattr(H, "_popen", lambda *args, **kwargs: 111)
    monkeypatch.setattr(H, "_listener_pid", lambda host, port: 222)
    monkeypatch.setattr(H, "_is_sonder_server_pid", lambda pid: True)

    out = H.start_sonder("127.0.0.1", 11435)

    assert "started pid=222" in out
    assert H.pid_file("sonder_serve").read_text(encoding="ascii") == "222"


def test_start_sonder_preserves_sonder_health_token(monkeypatch):
    token = "launcher-health-" + ("x" * 32)
    seen = {}
    monkeypatch.setenv("SONDER_LAUNCHER_HEALTH_TOKEN", token)
    monkeypatch.setattr(H, "port_open", lambda host, port: False)
    monkeypatch.setattr(H, "wait_until", lambda fn, seconds: True)
    monkeypatch.setattr(H, "python_exe", lambda: "python")
    monkeypatch.setattr(H, "runtime_environment", lambda: {"PATH": "fixture"})
    monkeypatch.setattr(H, "_listener_pid", lambda host, port: None)

    def fake_popen(command, name, env=None):
        seen["env"] = env
        return 123

    monkeypatch.setattr(H, "_popen", fake_popen)

    H.start_sonder("127.0.0.1", 11435)

    assert seen["env"]["SONDER_LAUNCHER_HEALTH_TOKEN"] == token


def test_start_sonder_preserves_only_managed_runtime_role(monkeypatch):
    seen = {}
    monkeypatch.setenv(H.sonder_health.ROLE_ENV, H.sonder_health.MANAGED_ROLE)
    monkeypatch.setattr(H, "port_open", lambda host, port: False)
    monkeypatch.setattr(H, "wait_until", lambda fn, seconds: True)
    monkeypatch.setattr(H, "python_exe", lambda: "python")
    monkeypatch.setattr(H, "runtime_environment", lambda: {"PATH": "fixture"})
    monkeypatch.setattr(H, "_listener_pid", lambda host, port: None)
    monkeypatch.setattr(
        H, "_popen", lambda command, name, env=None: seen.setdefault("env", env) or 123
    )
    H.start_sonder("127.0.0.1", 11435)
    assert seen["env"][H.sonder_health.ROLE_ENV] == H.sonder_health.MANAGED_ROLE


def test_generic_runtime_environment_strips_sonder_health_token(
    monkeypatch,
):
    monkeypatch.setenv("SONDER_LAUNCHER_HEALTH_TOKEN", "secret-" + ("x" * 32))
    monkeypatch.setenv(H.CONTROL_GATE_ENV, "1")
    monkeypatch.setattr(H.engine_bundle, "discover_engine_bundle", lambda root: None)

    env = H.runtime_environment()

    assert "SONDER_LAUNCHER_HEALTH_TOKEN" not in env
    assert H.CONTROL_GATE_ENV not in env


def test_runtime_probe_subprocess_strips_sonder_health_token(monkeypatch):
    token = "secret-" + ("x" * 32)
    seen = {}
    monkeypatch.setenv("SONDER_LAUNCHER_HEALTH_TOKEN", token)

    def fake_run(command, **kwargs):
        seen["env"] = kwargs["env"]
        return subprocess.CompletedProcess(command, 0, "Python 3", "")

    monkeypatch.setattr(H.subprocess, "run", fake_run)

    assert H._python_works("python-test") is True
    assert "SONDER_LAUNCHER_HEALTH_TOKEN" not in seen["env"]


def test_launcher_control_gate_requires_one_allow_byte(monkeypatch):
    monkeypatch.setenv(H.CONTROL_GATE_ENV, "1")
    monkeypatch.setattr(H.sys, "stdin", GateInput(H.CONTROL_GATE_ALLOW))
    assert H._launcher_control_gate() is True

    monkeypatch.setattr(H.sys, "stdin", GateInput(b""))
    assert H._launcher_control_gate() is False
    monkeypatch.setattr(H.sys, "stdin", GateInput(H.CONTROL_GATE_ALLOW + b"x"))
    assert H._launcher_control_gate() is False


def test_launcher_gate_eof_prevents_all_headless_work(monkeypatch):
    monkeypatch.setenv(H.CONTROL_GATE_ENV, "1")
    monkeypatch.setattr(H.sys, "stdin", GateInput(b""))
    monkeypatch.setattr(
        H,
        "start_ollama",
        lambda: (_ for _ in ()).throw(AssertionError("work must stay gated")),
    )
    assert H.main(["start"]) == 2


def test_engine_command_never_starts_api_server(monkeypatch):
    monkeypatch.delenv(H.CONTROL_GATE_ENV, raising=False)
    monkeypatch.setattr(H, "start_ollama", lambda: "ollama ready")
    monkeypatch.setattr(H, "ensure_sonder_alias", lambda: (True, "alias ready"))
    monkeypatch.setattr(
        H,
        "start_sonder",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("engine command must not start API")
        ),
    )

    assert H.main(["engine"]) == 0
