"""Headless Ollama + Sonder server supervisor.

This starts/stops/checks the local Ollama daemon and the OpenAI-compatible
sonder_serve.py API without requiring the Flutter app or a visible console.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import engine_bundle
import ollama_endpoint
import unsafe_lab
from sonder_runtime.adapters.process_liveness import pid_alive as _process_pid_alive
import sonder_health
import sonder_paths


DEFAULT_HOST = os.environ.get("SONDER_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("SONDER_PORT", "11435"))
CONTROL_GATE_ENV = "SONDER_LAUNCHER_CONTROL_GATE"
CONTROL_GATE_ALLOW = b"\x01"
_MAX_OLLAMA_TAGS_BYTES = 4 * 1024 * 1024


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


def _is_sonder_server_for_port(pid: int, port: int) -> bool:
    """Return whether a managed server PID belongs to ``port``.

    ``sonder_serve.pid`` is deliberately a simple, backwards-compatible PID
    file.  A user can nevertheless run another managed instance with a
    different port, at which point a stale PID file must never make ``stop
    --port`` terminate that other instance.  The supervisor always supplies
    the port as the script's first argument.  Preserve legacy default-port
    recognition for an older direct launch which omitted it.
    """
    command = _pid_command_line(pid)
    normalized = command.lower().replace("/", os.sep).replace("\\", os.sep)
    script = str((repo_root() / "sonder_serve.py").resolve()).lower()
    if not command or script not in normalized:
        return False
    match = re.search(r"sonder_serve\.py[\"']?\s+(\d+)(?:\s|$)", command, re.IGNORECASE)
    if match:
        return int(match.group(1)) == int(port)
    return int(port) == DEFAULT_PORT


def _managed_pid(name: str, host=DEFAULT_HOST, port=DEFAULT_PORT) -> int | None:
    """Resolve a service PID, repairing stale Windows venv-launcher pidfiles."""
    recorded = _read_pid(name)
    if recorded and pid_alive(recorded) and (
        name != "sonder_serve" or _is_sonder_server_for_port(recorded, port)
    ):
        return recorded
    if name != "sonder_serve" or not port_open(host, port):
        return None
    listener = _listener_pid(host, port)
    if listener and pid_alive(listener) and _is_sonder_server_for_port(listener, port):
        pid_file(name).write_text(str(listener), encoding="ascii")
        return listener
    return None


def _managed_listener_pid(host=DEFAULT_HOST, port=DEFAULT_PORT) -> int | None:
    """Return the managed Sonder PID only when it owns the listener.

    A TCP connect only proves that *something* accepted the connection.  In
    particular, a stale or unrelated local process can bind the port while a
    newly launched ``sonder_serve`` child is still starting (or has failed
    before bind).  Windows gives us the listening PID, so use that stronger
    identity evidence for readiness and status instead of combining a live
    child PID with an unrelated open port.

    The non-Windows fallback retains the existing managed-PID behaviour: this
    lightweight launcher deliberately has no platform-specific listener table
    implementation outside Windows.
    """
    if not port_open(host, port):
        return None
    listener = _listener_pid(host, port)
    if listener is not None:
        # A PID reported as a current TCP listener is already live; do not
        # perform a second process-table lookup that can race process exit.
        if _is_sonder_server_pid(listener):
            # Repair a stale launcher pidfile only after proving that this PID
            # owns the endpoint we are about to call ready.
            pid_file("sonder_serve").write_text(str(listener), encoding="ascii")
            return listener
        return None
    if os.name == "nt":
        # A listener exists but its owner could not be identified.  Do not
        # convert that weaker evidence into a successful Sonder launch.
        return None
    return _managed_pid("sonder_serve", host, port)


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


def ollama_client_environment() -> dict[str, str]:
    return ollama_endpoint.client_environment(runtime_environment())


def ollama_models() -> set[str] | None:
    """Return registered model names, or None when the gated endpoint is unavailable."""
    try:
        client_env = ollama_client_environment()
        request = urllib.request.Request(
            client_env["OLLAMA_HOST"] + "/api/tags", method="GET",
        )
        with ollama_endpoint.open_url(request, timeout=8) as response:
            raw = response.read(_MAX_OLLAMA_TAGS_BYTES + 1)
        if len(raw) > _MAX_OLLAMA_TAGS_BYTES:
            return None
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, TimeoutError, ValueError, urllib.error.URLError):
        return None
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return None
    names = set()
    for model in models:
        if not isinstance(model, dict):
            continue
        name = str(model.get("name") or model.get("model") or "").strip()
        if name:
            names.add(name)
    return names


def ollama_ok() -> bool:
    return ollama_models() is not None


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
    server_env = runtime_environment()
    try:
        client_env = ollama_endpoint.client_environment(server_env)
    except ValueError as exc:
        return "ollama: endpoint blocked - %s" % exc
    if not ollama_endpoint.is_loopback(client_env["OLLAMA_HOST"]):
        return "ollama: remote endpoint is unavailable; local daemon not started"
    pid = _popen([executable, "serve"], "ollama", env=server_env)
    if wait_until(ollama_ok, 12):
        return "ollama: started pid=%s" % pid
    return "ollama: start requested pid=%s, not reachable yet (see %s)" % (pid, log_file("ollama"))


def ensure_sonder_alias() -> tuple[bool, str]:
    models = ollama_models()
    if models is None:
        return False, "engine: Ollama is not reachable; alias setup skipped"
    if any(name.casefold() == "sonder:latest" for name in models):
        return True, "engine: sonder alias is ready"
    executable = ollama_exe()
    if not executable:
        return False, (
            "engine: Ollama is reachable but the sonder alias is missing; "
            "install the Ollama CLI to create it"
        )
    try:
        env = ollama_client_environment()
    except ValueError as exc:
        return False, "engine: Ollama endpoint blocked: %s" % exc
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


def _require_safe_unsafe_lab_start(host, overrides=None) -> bool:
    """Validate the exact environment a managed API child will receive.

    ``sonder_serve`` repeats this gate at bind time, but the headless command
    used to start/probe Ollama and bootstrap its alias first.  An invalid lab
    request must not get any sidecar work merely because rejection happens in
    the later API child.  Explicit launcher arguments win over stale parent
    variables, matching the child environment below.
    """
    effective = dict(os.environ)
    effective.update(dict(overrides or {}))
    effective["SONDER_HOST"] = str(host)
    return unsafe_lab.require_startup(env=effective, host=host)


def start_sonder(host=DEFAULT_HOST, port=DEFAULT_PORT, env=None) -> str:
    _require_safe_unsafe_lab_start(host, env)
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
    # ``wait_until`` can expire at the same instant the child binds the port;
    # perform one final direct probe before reporting a failed startup.  The
    # caller prints ``status`` immediately afterward, and without this probe a
    # healthy server can be labelled "did not start cleanly" one line before
    # that status says it is listening.
    if (wait_until(lambda: _managed_listener_pid(host, port) is not None, 12)
            or _managed_listener_pid(host, port) is not None):
        pid = _managed_listener_pid(host, port) or pid
        pid_file("sonder_serve").write_text(str(pid), encoding="ascii")
        return "sonder: started pid=%s at http://%s:%s" % (pid, host, port)
    return "sonder: start requested pid=%s, not reachable yet (see %s)" % (
        pid, log_file("sonder_serve"))


def stop_pid(name: str, host=DEFAULT_HOST, port=DEFAULT_PORT) -> str:
    recorded = _read_pid(name)
    if name == "sonder_serve":
        recorded_for_other_port = bool(
            recorded and pid_alive(recorded)
            and not _is_sonder_server_for_port(recorded, port)
        )
        candidates = ([] if recorded_for_other_port else [recorded]) + _listener_pids(host, port)
        pids = [
            pid for index, pid in enumerate(candidates)
            if pid and pid not in candidates[:index] and pid_alive(pid)
            and _is_sonder_server_for_port(pid, port)
        ]
    else:
        pids = [recorded] if recorded and pid_alive(recorded) else []
    if not pids:
        try:
            pid_file(name).unlink()
        except OSError:
            pass
        if recorded:
            if name == "sonder_serve" and recorded_for_other_port:
                return (
                    "%s: recorded pid %s is not for port %s; not stopped"
                    % (name, recorded, port)
                )
            return "%s: pid %s is not running" % (name, recorded)
        return "%s: no pid file" % name
    try:
        for pid in pids:
            if os.name == "nt":
                proc = subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    env=_child_environment(),
                )
                if proc.returncode != 0:
                    detail = (proc.stderr or proc.stdout or "taskkill failed").strip()
                    raise OSError(detail)
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


def _stop_succeeded(message: str) -> bool:
    return ": stop failed for pid=" not in message


def _start_succeeded(message: str) -> bool:
    # A false zero makes direct launchers and the desktop UI report a live
    # service even when no managed Sonder process owns the listening port.
    return message.startswith("sonder: started pid=") or (
        message.startswith("sonder: already listening")
        and "(unmanaged listener)" not in message
    )


def status(host=DEFAULT_HOST, port=DEFAULT_PORT) -> str:
    listener_open = port_open(host, port)
    sonder_pid = _managed_listener_pid(host, port)
    if sonder_pid:
        api_status = "listening on http://%s:%s (pid %s)" % (
            host, port, sonder_pid,
        )
    elif listener_open:
        api_status = "unmanaged listener on http://%s:%s (not Sonder)" % (host, port)
    else:
        api_status = "not listening"
    lines = [
        "sonder headless status",
        "  ollama: %s%s" % (
            "reachable" if ollama_ok() else "not reachable",
            " (pid %s)" % _read_pid("ollama") if _read_pid("ollama") else "",
        ),
        "  sonder api: %s" % api_status,
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
    parser.add_argument(
        "command", nargs="?", default="start",
        choices=["start", "engine", "status", "stop", "restart"],
    )
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
        messages = [stop_pid("sonder_serve", args.host, args.port)]
        print(messages[0])
        if args.stop_ollama:
            messages.append(stop_pid("ollama"))
            print(messages[-1])
        return 0 if all(_stop_succeeded(message) for message in messages) else 1
    if args.command == "restart":
        stopped = stop_pid("sonder_serve", args.host, args.port)
        print(stopped)
        if not _stop_succeeded(stopped):
            return 1
    try:
        # Validate before touching Ollama or the bootstrap subprocess.  The
        # API child validates again at bind time; this protects the launcher
        # sequence itself and includes --allow-hosted in the effective env.
        _require_safe_unsafe_lab_start(args.host, env)
    except unsafe_lab.UnsafeLabError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2
    print(start_ollama())
    alias_ok, alias_message = ensure_sonder_alias()
    print(alias_message)
    if not alias_ok:
        return 2
    if args.command == "engine":
        return 0
    started = start_sonder(args.host, args.port, env=env)
    print(started)
    print(status(args.host, args.port))
    # A cold Python process can bind just after ``start_sonder`` exhausts its
    # bounded readiness window.  ``status`` above probes the same endpoint
    # immediately afterward, so preserve a successful exit when that probe
    # can verify the listener belongs to our managed Sonder process.  Do not
    # treat an arbitrary unmanaged listener as success.
    managed = _managed_listener_pid(args.host, args.port)
    if not managed:
        # A Windows process can accept the readiness probe just before its
        # listener ownership becomes observable through ``netstat``.  Give
        # that identity check one small, bounded chance to converge; a bare
        # open port still never becomes launch success.
        wait_until(lambda: _managed_listener_pid(args.host, args.port) is not None, 2)
        managed = _managed_listener_pid(args.host, args.port)
    # ``_managed_listener_pid`` proves both ownership and the listener, so a
    # hung/pre-bind child still returns a failure to launchers and the desktop
    # shell.
    return 0 if _start_succeeded(started) or managed else 1


if __name__ == "__main__":
    raise SystemExit(main())
