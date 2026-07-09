"""Headless Ollama + Trilobite server supervisor.

This starts/stops/checks the local Ollama daemon and the OpenAI-compatible
trilobite_serve.py API without requiring the Flutter app or a visible console.
"""
from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import trilobite_paths


DEFAULT_HOST = os.environ.get("TRILOBITE_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("TRILOBITE_PORT", "11435"))


def repo_root() -> Path:
    return Path(__file__).resolve().parent


def run_dir() -> Path:
    path = Path(trilobite_paths.default_home()) / "run"
    path.mkdir(parents=True, exist_ok=True)
    return path


def python_exe() -> str:
    venv = repo_root() / "venv" / "Scripts" / "python.exe"
    if venv.exists() and _python_works(str(venv)):
        return str(venv)
    return sys.executable or "python"


def _python_works(path: str) -> bool:
    try:
        proc = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def pid_file(name: str) -> Path:
    return run_dir() / ("%s.pid" % name)


def log_file(name: str) -> Path:
    return run_dir() / ("%s.log" % name)


def _creationflags() -> int:
    if os.name == "nt":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    return 0


def _popen(cmd, name: str, env=None):
    log = log_file(name).open("ab")
    proc = subprocess.Popen(
        cmd,
        cwd=str(repo_root()),
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        env=env or os.environ.copy(),
        creationflags=_creationflags(),
        close_fds=(os.name != "nt"),
    )
    pid_file(name).write_text(str(proc.pid), encoding="ascii")
    return proc.pid


def _read_pid(name: str) -> int | None:
    try:
        return int(pid_file(name).read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def port_open(host=DEFAULT_HOST, port=DEFAULT_PORT, timeout=0.5) -> bool:
    connect_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    try:
        with socket.create_connection((connect_host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def ollama_ok() -> bool:
    try:
        proc = subprocess.run(
            ["ollama", "list"],
            capture_output=True,
            text=True,
            timeout=8,
        )
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def wait_until(fn, seconds: float) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if fn():
            return True
        time.sleep(0.25)
    return fn()


def start_ollama() -> str:
    if ollama_ok():
        return "ollama: already reachable"
    if not shutil.which("ollama"):
        return "ollama: not installed or not on PATH"
    pid = _popen(["ollama", "serve"], "ollama")
    if wait_until(ollama_ok, 12):
        return "ollama: started pid=%s" % pid
    return "ollama: start requested pid=%s, not reachable yet (see %s)" % (pid, log_file("ollama"))


def start_trilobite(host=DEFAULT_HOST, port=DEFAULT_PORT, env=None) -> str:
    if port_open(host, port):
        return "trilobite: already listening on http://%s:%s" % (host, port)
    cmd = [python_exe(), str(repo_root() / "trilobite_serve.py"), str(port)]
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    merged_env.setdefault("TRILOBITE_HOST", host)
    merged_env.setdefault("TRILOBITE_PORT", str(port))
    pid = _popen(cmd, "trilobite_serve", env=merged_env)
    if wait_until(lambda: port_open(host, port), 12):
        return "trilobite: started pid=%s at http://%s:%s" % (pid, host, port)
    return "trilobite: start requested pid=%s, not reachable yet (see %s)" % (
        pid, log_file("trilobite_serve"))


def stop_pid(name: str) -> str:
    pid = _read_pid(name)
    if not pid:
        return "%s: no pid file" % name
    if not pid_alive(pid):
        try:
            pid_file(name).unlink()
        except OSError:
            pass
        return "%s: pid %s is not running" % (name, pid)
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True, timeout=10)
        else:
            os.kill(pid, 15)
        try:
            pid_file(name).unlink()
        except OSError:
            pass
        return "%s: stopped pid=%s" % (name, pid)
    except OSError as exc:
        return "%s: stop failed for pid=%s: %s" % (name, pid, exc)


def status(host=DEFAULT_HOST, port=DEFAULT_PORT) -> str:
    lines = [
        "trilobite headless status",
        "  ollama: %s%s" % (
            "reachable" if ollama_ok() else "not reachable",
            " (pid %s)" % _read_pid("ollama") if _read_pid("ollama") else "",
        ),
        "  trilobite api: %s%s" % (
            "listening on http://%s:%s" % (host, port) if port_open(host, port) else "not listening",
            " (pid %s)" % _read_pid("trilobite_serve") if _read_pid("trilobite_serve") else "",
        ),
        "  run dir: %s" % run_dir(),
        "  logs: %s, %s" % (log_file("ollama"), log_file("trilobite_serve")),
    ]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run Trilobite + Ollama headlessly.")
    parser.add_argument("command", nargs="?", default="start", choices=["start", "status", "stop", "restart"])
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--stop-ollama", action="store_true", help="Also stop Ollama when stopping.")
    parser.add_argument("--context-size", default=os.environ.get("TRILOBITE_CONTEXT_SIZE", ""))
    parser.add_argument("--allow-hosted", action="store_true")
    args = parser.parse_args(argv)

    env = {}
    if args.context_size:
        env["TRILOBITE_CONTEXT_SIZE"] = args.context_size
    if args.allow_hosted:
        env["TRILOBITE_ALLOW_CLOUD"] = "1"

    if args.command == "status":
        print(status(args.host, args.port))
        return 0
    if args.command == "stop":
        print(stop_pid("trilobite_serve"))
        if args.stop_ollama:
            print(stop_pid("ollama"))
        return 0
    if args.command == "restart":
        print(stop_pid("trilobite_serve"))
    print(start_ollama())
    print(start_trilobite(args.host, args.port, env=env))
    print(status(args.host, args.port))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

