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
    if os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, int(pid))
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                return bool(
                    kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
                    and exit_code.value == 259  # STILL_ACTIVE
                )
            finally:
                kernel32.CloseHandle(handle)
        except (AttributeError, OSError, ValueError):
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


def _listener_pids(host=DEFAULT_HOST, port=DEFAULT_PORT) -> list[int]:
    """Return Windows PIDs listening on ``port`` without extra packages."""
    if os.name != "nt":
        return []
    try:
        proc = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    found = []
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP" or parts[3].upper() != "LISTENING":
            continue
        try:
            local_port = int(parts[1].rsplit(":", 1)[1])
            pid = int(parts[4])
        except (IndexError, ValueError):
            continue
        if local_port == int(port) and pid not in found:
            found.append(pid)
    return found


def _listener_pid(host=DEFAULT_HOST, port=DEFAULT_PORT) -> int | None:
    listeners = _listener_pids(host, port)
    return listeners[0] if listeners else None


def _pid_command_line(pid: int) -> str:
    if os.name == "nt":
        command = (
            "$p=Get-CimInstance Win32_Process -Filter 'ProcessId = %d'; "
            "if($p){[Console]::Out.Write($p.CommandLine)}" % int(pid)
        )
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command", command],
                capture_output=True,
                text=True,
                timeout=8,
            )
            return proc.stdout.strip() if proc.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            return ""
    try:
        return Path("/proc/%d/cmdline" % int(pid)).read_bytes().replace(b"\0", b" ").decode(
            "utf-8", errors="replace"
        )
    except OSError:
        return ""


def _is_trilobite_server_pid(pid: int) -> bool:
    command = _pid_command_line(pid).lower().replace("/", os.sep).replace("\\", os.sep)
    script = str((repo_root() / "trilobite_serve.py").resolve()).lower()
    return bool(command and script in command)


def _managed_pid(name: str, host=DEFAULT_HOST, port=DEFAULT_PORT) -> int | None:
    """Resolve a service PID, repairing stale Windows venv-launcher pidfiles."""
    recorded = _read_pid(name)
    if recorded and pid_alive(recorded) and (
        name != "trilobite_serve" or _is_trilobite_server_pid(recorded)
    ):
        return recorded
    if name != "trilobite_serve" or not port_open(host, port):
        return None
    listener = _listener_pid(host, port)
    if listener and pid_alive(listener) and _is_trilobite_server_pid(listener):
        pid_file(name).write_text(str(listener), encoding="ascii")
        return listener
    return None


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
        pid = _managed_pid("trilobite_serve", host, port)
        suffix = " pid=%s" % pid if pid else " (unmanaged listener)"
        return "trilobite: already listening on http://%s:%s%s" % (host, port, suffix)
    cmd = [python_exe(), str(repo_root() / "trilobite_serve.py"), str(port)]
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    merged_env.setdefault("TRILOBITE_HOST", host)
    merged_env.setdefault("TRILOBITE_PORT", str(port))
    pid = _popen(cmd, "trilobite_serve", env=merged_env)
    if wait_until(lambda: port_open(host, port), 12):
        listener = _listener_pid(host, port)
        if listener and _is_trilobite_server_pid(listener):
            pid = listener
            pid_file("trilobite_serve").write_text(str(pid), encoding="ascii")
        return "trilobite: started pid=%s at http://%s:%s" % (pid, host, port)
    return "trilobite: start requested pid=%s, not reachable yet (see %s)" % (
        pid, log_file("trilobite_serve"))


def stop_pid(name: str, host=DEFAULT_HOST, port=DEFAULT_PORT) -> str:
    recorded = _read_pid(name)
    if name == "trilobite_serve":
        candidates = ([recorded] if recorded else []) + _listener_pids(host, port)
        pids = [
            pid for index, pid in enumerate(candidates)
            if pid and pid not in candidates[:index] and pid_alive(pid)
            and _is_trilobite_server_pid(pid)
        ]
    else:
        pids = [recorded] if recorded and pid_alive(recorded) else []
    if not pids:
        try:
            pid_file(name).unlink()
        except OSError:
            pass
        if recorded:
            return "%s: pid %s is not running" % (name, recorded)
        return "%s: no pid file" % name
    try:
        for pid in pids:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True, timeout=10)
            else:
                os.kill(pid, 15)
        try:
            pid_file(name).unlink()
        except OSError:
            pass
        return "%s: stopped pid=%s" % (name, ",".join(str(pid) for pid in pids))
    except OSError as exc:
        return "%s: stop failed for pid=%s: %s" % (
            name, ",".join(str(pid) for pid in pids), exc,
        )


def status(host=DEFAULT_HOST, port=DEFAULT_PORT) -> str:
    trilobite_pid = _managed_pid("trilobite_serve", host, port)
    lines = [
        "trilobite headless status",
        "  ollama: %s%s" % (
            "reachable" if ollama_ok() else "not reachable",
            " (pid %s)" % _read_pid("ollama") if _read_pid("ollama") else "",
        ),
        "  trilobite api: %s%s" % (
            "listening on http://%s:%s" % (host, port) if port_open(host, port) else "not listening",
            " (pid %s)" % trilobite_pid if trilobite_pid else "",
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
        print(stop_pid("trilobite_serve", args.host, args.port))
        if args.stop_ollama:
            print(stop_pid("ollama"))
        return 0
    if args.command == "restart":
        print(stop_pid("trilobite_serve", args.host, args.port))
    print(start_ollama())
    print(start_trilobite(args.host, args.port, env=env))
    print(status(args.host, args.port))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

