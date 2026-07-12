"""Headless Ollama + Sonder server supervisor.

This starts/stops/checks the local Ollama daemon and the OpenAI-compatible
sonder_serve.py API without requiring the Flutter app or a visible console.
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

import engine_bundle
from process_liveness import pid_alive as _process_pid_alive
import sonder_health
import sonder_paths


DEFAULT_HOST = os.environ.get("SONDER_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("SONDER_PORT", "11435"))
CONTROL_GATE_ENV = "SONDER_LAUNCHER_CONTROL_GATE"
CONTROL_GATE_ALLOW = b"\x01"


def _child_environment() -> dict[str, str]:
    """Return inherited state without the server-only health proof secret."""
    env = os.environ.copy()
    env.pop(sonder_health.TOKEN_ENV, None)
    env.pop(sonder_health.ROLE_ENV, None)
    env.pop(CONTROL_GATE_ENV, None)
    return env


def repo_root() -> Path:
    return Path(__file__).resolve().parent


def run_dir() -> Path:
    path = Path(sonder_paths.default_home()) / "run"
    path.mkdir(parents=True, exist_ok=True)
    return path


def python_exe() -> str:
    configured = os.environ.get("SONDER_PYTHON", "").strip()
    if configured and _python_works(configured):
        return configured
    try:
        bundle = engine_bundle.discover_engine_bundle(repo_root())
    except ValueError:
        bundle = None
    if bundle is not None and _python_works(str(bundle.python_executable)):
        return str(bundle.python_executable)
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
            env=_child_environment(),
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
        env=env if env is not None else _child_environment(),
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
    return _process_pid_alive(pid)


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
            env=_child_environment(),
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
                env=_child_environment(),
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


def _is_sonder_server_pid(pid: int) -> bool:
    command = _pid_command_line(pid).lower().replace("/", os.sep).replace("\\", os.sep)
    script = str((repo_root() / "sonder_serve.py").resolve()).lower()
    return bool(command and script in command)


def _managed_pid(name: str, host=DEFAULT_HOST, port=DEFAULT_PORT) -> int | None:
    """Resolve a service PID, repairing stale Windows venv-launcher pidfiles."""
    recorded = _read_pid(name)
    if recorded and pid_alive(recorded) and (
        name != "sonder_serve" or _is_sonder_server_pid(recorded)
    ):
        return recorded
    if name != "sonder_serve" or not port_open(host, port):
        return None
    listener = _listener_pid(host, port)
    if listener and pid_alive(listener) and _is_sonder_server_pid(listener):
        pid_file(name).write_text(str(listener), encoding="ascii")
        return listener
    return None


def ollama_exe() -> str:
    configured = os.environ.get("SONDER_OLLAMA_EXE", "").strip()
    if configured:
        return configured
    try:
        bundle = engine_bundle.discover_engine_bundle(repo_root())
    except ValueError:
        bundle = None
    if bundle is not None:
        return str(bundle.ollama_executable)
    return shutil.which("ollama") or ""


def runtime_environment() -> dict[str, str]:
    env = _child_environment()
    try:
        bundle = engine_bundle.discover_engine_bundle(repo_root())
    except ValueError:
        bundle = None
    if bundle is not None:
        model_store = engine_bundle.default_sonder_home() / "ollama-models"
        env.update(engine_bundle.runtime_environment(bundle, model_store))
    # The launcher proof secret belongs only to sonder_serve.py. Ollama,
    # bootstrap, model inspection, and conversion children must never inherit it.
    env.pop(sonder_health.TOKEN_ENV, None)
    env.pop(sonder_health.ROLE_ENV, None)
    return env


def ollama_ok() -> bool:
    executable = ollama_exe()
    if not executable:
        return False
    try:
        proc = subprocess.run(
            [executable, "list"],
            capture_output=True,
            text=True,
            timeout=8,
            env=runtime_environment(),
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
    executable = ollama_exe()
    if not executable:
        return "ollama: not installed or not on PATH"
    pid = _popen([executable, "serve"], "ollama", env=runtime_environment())
    if wait_until(ollama_ok, 12):
        return "ollama: started pid=%s" % pid
    return "ollama: start requested pid=%s, not reachable yet (see %s)" % (pid, log_file("ollama"))


def ensure_sonder_alias() -> tuple[bool, str]:
    executable = ollama_exe()
    if not executable or not ollama_ok():
        return False, "engine: Ollama is not reachable; alias setup skipped"
    env = runtime_environment()
    try:
        probe = subprocess.run(
            [executable, "show", "sonder"],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, "engine: could not inspect sonder alias: %s" % exc
    if probe.returncode == 0:
        return True, "engine: sonder alias is ready"
    command = [python_exe(), str(repo_root() / "bootstrap_engine.py")]
    try:
        bundle = engine_bundle.discover_engine_bundle(repo_root())
    except ValueError as exc:
        return False, "engine: bundled runtime is invalid: %s" % exc
    if bundle is not None:
        command.append("--offline")
    try:
        setup = subprocess.run(
            command,
            cwd=str(repo_root()),
            env=env,
            capture_output=True,
            text=True,
            timeout=30 * 60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, "engine: bootstrap could not run: %s" % exc
    if setup.returncode == 0:
        return True, "engine: bootstrap completed"
    detail = (setup.stderr or setup.stdout).strip()
    if len(detail) > 600:
        detail = detail[-600:]
    return False, "engine: bootstrap failed (%s)%s" % (
        setup.returncode,
        ": " + detail if detail else "",
    )


def start_sonder(host=DEFAULT_HOST, port=DEFAULT_PORT, env=None) -> str:
    if port_open(host, port):
        pid = _managed_pid("sonder_serve", host, port)
        suffix = " pid=%s" % pid if pid else " (unmanaged listener)"
        return "sonder: already listening on http://%s:%s%s" % (host, port, suffix)
    cmd = [python_exe(), str(repo_root() / "sonder_serve.py"), str(port)]
    merged_env = runtime_environment()
    child_overrides = dict(env or {})
    health_token = child_overrides.pop(
        sonder_health.TOKEN_ENV,
        os.environ.get(sonder_health.TOKEN_ENV, ""),
    )
    runtime_role = child_overrides.pop(
        sonder_health.ROLE_ENV,
        os.environ.get(sonder_health.ROLE_ENV, ""),
    )
    merged_env.update(child_overrides)
    if health_token:
        # Explicitly restore the proof secret only for the managed API child.
        merged_env[sonder_health.TOKEN_ENV] = health_token
    else:
        merged_env.pop(sonder_health.TOKEN_ENV, None)
    if runtime_role == sonder_health.MANAGED_ROLE:
        merged_env[sonder_health.ROLE_ENV] = runtime_role
    else:
        merged_env.pop(sonder_health.ROLE_ENV, None)
    merged_env.setdefault("SONDER_HOST", host)
    merged_env.setdefault("SONDER_PORT", str(port))
    pid = _popen(cmd, "sonder_serve", env=merged_env)
    if wait_until(lambda: port_open(host, port), 12):
        listener = _listener_pid(host, port)
        if listener and _is_sonder_server_pid(listener):
            pid = listener
            pid_file("sonder_serve").write_text(str(pid), encoding="ascii")
        return "sonder: started pid=%s at http://%s:%s" % (pid, host, port)
    return "sonder: start requested pid=%s, not reachable yet (see %s)" % (
        pid, log_file("sonder_serve"))


def stop_pid(name: str, host=DEFAULT_HOST, port=DEFAULT_PORT) -> str:
    recorded = _read_pid(name)
    if name == "sonder_serve":
        candidates = ([recorded] if recorded else []) + _listener_pids(host, port)
        pids = [
            pid for index, pid in enumerate(candidates)
            if pid and pid not in candidates[:index] and pid_alive(pid)
            and _is_sonder_server_pid(pid)
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
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    env=_child_environment(),
                )
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
    sonder_pid = _managed_pid("sonder_serve", host, port)
    lines = [
        "sonder headless status",
        "  ollama: %s%s" % (
            "reachable" if ollama_ok() else "not reachable",
            " (pid %s)" % _read_pid("ollama") if _read_pid("ollama") else "",
        ),
        "  sonder api: %s%s" % (
            "listening on http://%s:%s" % (host, port) if port_open(host, port) else "not listening",
            " (pid %s)" % sonder_pid if sonder_pid else "",
        ),
        "  run dir: %s" % run_dir(),
        "  logs: %s, %s" % (log_file("ollama"), log_file("sonder_serve")),
    ]
    return "\n".join(lines)


def _launcher_control_gate() -> bool:
    """Block launcher-owned commands until ownership is durably recorded."""
    if os.environ.get(CONTROL_GATE_ENV) != "1":
        return True
    try:
        supplied = sys.stdin.buffer.read(2)
    except (AttributeError, OSError):
        supplied = b""
    if supplied == CONTROL_GATE_ALLOW:
        return True
    print("ERROR: launcher control gate was not released", file=sys.stderr)
    return False


def main(argv=None) -> int:
    if not _launcher_control_gate():
        return 2
    parser = argparse.ArgumentParser(description="Run Sonder Runtime and Ollama headlessly.")
    parser.add_argument("command", nargs="?", default="start", choices=["start", "status", "stop", "restart"])
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--stop-ollama", action="store_true", help="Also stop Ollama when stopping.")
    parser.add_argument("--context-size", default=os.environ.get("SONDER_CONTEXT_SIZE", ""))
    parser.add_argument("--allow-hosted", action="store_true")
    args = parser.parse_args(argv)

    env = {}
    if args.context_size:
        env["SONDER_CONTEXT_SIZE"] = args.context_size
    if args.allow_hosted:
        env["SONDER_ALLOW_CLOUD"] = "1"

    if args.command == "status":
        print(status(args.host, args.port))
        return 0
    if args.command == "stop":
        print(stop_pid("sonder_serve", args.host, args.port))
        if args.stop_ollama:
            print(stop_pid("ollama"))
        return 0
    if args.command == "restart":
        print(stop_pid("sonder_serve", args.host, args.port))
    print(start_ollama())
    alias_ok, alias_message = ensure_sonder_alias()
    print(alias_message)
    if not alias_ok:
        return 2
    print(start_sonder(args.host, args.port, env=env))
    print(status(args.host, args.port))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
