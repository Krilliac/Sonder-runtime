import subprocess

import trilobite_headless as H


def test_pid_file_paths_use_run_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(H, "run_dir", lambda: tmp_path)

    assert H.pid_file("x") == tmp_path / "x.pid"
    assert H.log_file("x") == tmp_path / "x.log"


def test_start_trilobite_skips_when_port_open(monkeypatch):
    monkeypatch.setattr(H, "port_open", lambda host, port: True)

    out = H.start_trilobite("127.0.0.1", 11435)

    assert "already listening" in out


def test_start_ollama_skips_when_already_reachable(monkeypatch):
    monkeypatch.setattr(H, "ollama_ok", lambda: True)

    assert H.start_ollama() == "ollama: already reachable"


def test_start_ollama_reports_missing_binary(monkeypatch):
    monkeypatch.setattr(H, "ollama_ok", lambda: False)
    monkeypatch.setattr(H.shutil, "which", lambda exe: None)

    assert H.start_ollama() == "ollama: not installed or not on PATH"


def test_python_exe_ignores_broken_venv(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    venv = root / "venv" / "Scripts"
    venv.mkdir(parents=True)
    (venv / "python.exe").write_text("broken", encoding="utf-8")
    monkeypatch.setattr(H, "repo_root", lambda: root)
    monkeypatch.setattr(H, "_python_works", lambda path: False)
    monkeypatch.setattr(H.sys, "executable", "C:/Python/python.exe")

    assert H.python_exe() == "C:/Python/python.exe"


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
    H.pid_file("trilobite_serve").write_text("111", encoding="ascii")
    monkeypatch.setattr(H, "pid_alive", lambda pid: pid == 222)
    monkeypatch.setattr(H, "port_open", lambda host, port: True)
    monkeypatch.setattr(H, "_listener_pid", lambda host, port: 222)
    monkeypatch.setattr(H, "_is_trilobite_server_pid", lambda pid: pid == 222)

    assert H._managed_pid("trilobite_serve") == 222
    assert H.pid_file("trilobite_serve").read_text(encoding="ascii") == "222"


def test_start_trilobite_records_real_listener_pid(monkeypatch, tmp_path):
    monkeypatch.setattr(H, "run_dir", lambda: tmp_path)
    monkeypatch.setattr(H, "port_open", lambda host, port: False)
    monkeypatch.setattr(H, "wait_until", lambda fn, seconds: True)
    monkeypatch.setattr(H, "python_exe", lambda: "python")
    monkeypatch.setattr(H, "_popen", lambda *args, **kwargs: 111)
    monkeypatch.setattr(H, "_listener_pid", lambda host, port: 222)
    monkeypatch.setattr(H, "_is_trilobite_server_pid", lambda pid: True)

    out = H.start_trilobite("127.0.0.1", 11435)

    assert "started pid=222" in out
    assert H.pid_file("trilobite_serve").read_text(encoding="ascii") == "222"
