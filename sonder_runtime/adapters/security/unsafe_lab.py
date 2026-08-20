"""Stateful unsafe-lab security adapter.

The adapter is the sole owner of host inspection, process-local audit state,
and audit-file I/O.  The root ``unsafe_lab`` module aliases this object so
legacy callers and their monkeypatch surfaces retain identity semantics.
"""
from __future__ import annotations

import ctypes
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from sonder_runtime.platform import unsafe_lab_policy

ACK_ENV = unsafe_lab_policy.ACK_ENV
ACKNOWLEDGEMENT = unsafe_lab_policy.ACKNOWLEDGEMENT
AUDIT_PATH_ENV = "SONDER_UNSAFE_LAB_AUDIT_PATH"
WARNING = (
    "DANGER: UNSAFE LAB MODE IS ACTIVE; MODEL AGENT/AUTOPILOT HOST-TOOL "
    "RESTRICTIONS ARE DISABLED. THIS IS NOT OS ISOLATION."
)


class UnsafeLabError(RuntimeError):
    """The operator requested unsafe mode but a mandatory gate failed."""


@dataclass(frozen=True)
class State:
    requested: bool
    enabled: bool
    error: str = ""
    host: str = ""


_audit_lock = threading.Lock()
_audited_processes: set[int] = set()


def _windows_elevated() -> bool:
    """Return the token elevation bit; failure is conservatively privileged."""
    try:
        token = ctypes.c_void_p()
        token_query = 0x0008
        if not ctypes.windll.advapi32.OpenProcessToken(  # type: ignore[attr-defined]
            ctypes.windll.kernel32.GetCurrentProcess(), token_query, ctypes.byref(token)
        ):
            return True
        try:
            elevation = ctypes.c_ulong()
            returned = ctypes.c_ulong()
            token_elevation = 20
            ok = ctypes.windll.advapi32.GetTokenInformation(  # type: ignore[attr-defined]
                token, token_elevation, ctypes.byref(elevation), ctypes.sizeof(elevation), ctypes.byref(returned)
            )
            return True if not ok else bool(elevation.value)
        finally:
            ctypes.windll.kernel32.CloseHandle(token)  # type: ignore[attr-defined]
    except Exception:
        return True


def is_privileged() -> bool:
    if os.name == "nt":
        return _windows_elevated()
    geteuid = getattr(os, "geteuid", None)
    return True if geteuid is None else geteuid() == 0


def inspect(*, env=None, host=None, privilege_probe=None) -> State:
    source = os.environ if env is None else env
    if ACK_ENV not in source:
        return State(requested=False, enabled=False)
    configured_host = str(
        host if host is not None else source.get("SONDER_HOST", "127.0.0.1")
    ).strip()
    error = unsafe_lab_policy.validation_error(source, host=host)
    if error:
        return State(requested=True, enabled=False, error=error, host=configured_host)
    probe = is_privileged if privilege_probe is None else privilege_probe
    if probe():
        return State(
            requested=True,
            enabled=False,
            error="unsafe lab mode refuses root or elevated/administrator execution",
            host=configured_host,
        )
    return State(requested=True, enabled=True, host=configured_host)


def _audit_path(env) -> Path:
    explicit = str(env.get(AUDIT_PATH_ENV, "")).strip()
    if explicit:
        return Path(explicit).expanduser()
    home = str(env.get("SONDER_HOME", "")).strip()
    if home:
        return Path(home).expanduser() / "audit" / "unsafe-lab.jsonl"
    if os.name == "nt":
        base = env.get("LOCALAPPDATA") or env.get("USERPROFILE") or os.getcwd()
        return Path(base) / "sonder" / "audit" / "unsafe-lab.jsonl"
    base = env.get("XDG_STATE_HOME")
    if base:
        return Path(base) / "sonder" / "unsafe-lab.jsonl"
    return Path(env.get("HOME") or os.getcwd()) / ".local" / "state" / "sonder" / "unsafe-lab.jsonl"


def _record_activation(state: State, env) -> None:
    pid = os.getpid()
    with _audit_lock:
        if pid in _audited_processes:
            return
        path = _audit_path(env)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"event": "unsafe_lab_activated", "host": state.host, "pid": pid, "time_unix": int(time.time()), "warning": WARNING}
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        if os.name == "posix":
            path.chmod(0o600)
        _audited_processes.add(pid)


def require_startup(*, env=None, host=None, privilege_probe=None, audit=True) -> bool:
    source = os.environ if env is None else env
    state = inspect(env=source, host=host, privilege_probe=privilege_probe)
    if state.error:
        raise UnsafeLabError(state.error)
    if state.enabled and audit:
        _record_activation(state, source)
    return state.enabled


def active() -> bool:
    """Return activation state, raising when an attempted activation is invalid."""
    return require_startup()


def status_line(*, env=None, host=None, privilege_probe=None) -> str:
    state = inspect(env=env, host=host, privilege_probe=privilege_probe)
    if state.enabled:
        return WARNING
    if state.error:
        return "REFUSED: " + state.error
    return "off (safe default; agent/autopilot host-tool policy enforced)"
