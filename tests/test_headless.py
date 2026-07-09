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
