"""Minimal authenticated supervisor for starting Sonder from mobile apps.

This process is intentionally independent from server.py and exposes only
status/start/stop/restart. It is not a shell and accepts no executable paths or
arbitrary arguments from clients.
"""
from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
import errno
import hmac
import json
import os
import re
import secrets
import signal
import socket
import sqlite3
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import sonder_health
from process_liveness import pid_alive as _process_pid_alive
from sonder_paths import state_path


ROOT = Path(__file__).resolve().parent
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 11436
SERVER_PORT = 11435
MAX_BODY = 16_384
MAX_CONTEXT_TOKENS = 1_000_000
START_ACTION_TIMEOUT_SECONDS = 31 * 60.0
STOP_ACTION_TIMEOUT_SECONDS = 60.0
# Kept for callers that imported the old single timeout constant. Start and
# restart are the longest bounded operations; stop has its own smaller cap.
ACTION_TIMEOUT_SECONDS = START_ACTION_TIMEOUT_SECONDS
POLL_INTERVAL_SECONDS = 0.25
BODY_READ_TIMEOUT_SECONDS = 5.0
MAX_OPERATION_OUTPUT = 20_000
DEFAULT_OPERATION_RETENTION = 100
MAX_OPERATION_RETENTION = 1_000
LOCK_GRACE_SECONDS = 15.0
CONTROL_GATE_ENV = "SONDER_LAUNCHER_CONTROL_GATE"
CONTROL_TERMINATE_GRACE_SECONDS = 2.0
CONTROL_KILL_GRACE_SECONDS = 3.0
FINALIZE_RETRY_DELAYS = (0.0, 0.05, 0.2)
ACTIVE_PHASES = ("queued", "running")
TERMINAL_PHASES = ("succeeded", "failed", "cancelled", "interrupted")
_CONTEXT_SIZE = re.compile(r"^(\d{1,7})(?:\.(\d{1,3}))?([km]?)$")
_OPERATION_ID = re.compile(r"^[0-9a-f]{32}$")
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sonder_launcher_operations (
    id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    context_size TEXT NOT NULL,
    phase TEXT NOT NULL,
    idempotency_key TEXT UNIQUE,
    created_ts REAL NOT NULL,
    started_ts REAL,
    updated_ts REAL NOT NULL,
    finished_ts REAL,
    owner_id TEXT NOT NULL,
    owner_pid INTEGER NOT NULL,
    owner_host TEXT NOT NULL,
    lease_until REAL NOT NULL,
    message TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    commands_json TEXT NOT NULL DEFAULT '[]',
    hard_deadline REAL,
    control_pid INTEGER,
    control_identity TEXT,
    control_group_id INTEGER,
    control_platform TEXT,
    control_started_ts REAL,
    control_finished_ts REAL,
    control_exit_code INTEGER
);
CREATE INDEX IF NOT EXISTS sonder_launcher_operations_phase_created
    ON sonder_launcher_operations(phase, created_ts DESC);
CREATE TABLE IF NOT EXISTS sonder_launcher_operation_lock (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    operation_id TEXT,
    owner_id TEXT,
    owner_pid INTEGER,
    owner_host TEXT,
    lease_until REAL
);
INSERT OR IGNORE INTO sonder_launcher_operation_lock(id) VALUES (1);
"""

_OPERATION_MIGRATIONS = {
    "hard_deadline": "REAL",
    "control_pid": "INTEGER",
    "control_identity": "TEXT",
    "control_group_id": "INTEGER",
    "control_platform": "TEXT",
    "control_started_ts": "REAL",
    "control_finished_ts": "REAL",
    "control_exit_code": "INTEGER",
}

_LIVE_WORKERS_LOCK = threading.Lock()
_LIVE_WORKERS = {}


class LauncherConflictError(RuntimeError):
    """A second operation or mismatched idempotent replay was rejected."""

    def __init__(self, message, operation=None):
        super().__init__(message)
        self.operation = operation


class ControlTreeNotStopped(RuntimeError):
    """The launcher must retain its lock because owned control work survived."""


def _loopback(host):
    value = str(host or "").strip().strip("[]").lower()
    if value in {"localhost", "::1"}:
        return True
    try:
        return socket.gethostbyname(value).startswith("127.") or value == "::1"
    except OSError:
        return False


def _reachable(host="127.0.0.1", port=SERVER_PORT, timeout=0.4):
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def normalize_context_size(value):
    """Validate the bounded context syntax accepted by the main server."""
    text = str(value or "8192").strip().lower()
    match = _CONTEXT_SIZE.fullmatch(text)
    if not match:
        raise ValueError("invalid context_size")
    try:
        number = Decimal(match.group(1) + ("." + match.group(2) if match.group(2) else ""))
    except InvalidOperation as exc:  # Defensive: the regular expression is stricter.
        raise ValueError("invalid context_size") from exc
    multiplier = {"": 1, "k": 1_000, "m": 1_000_000}[match.group(3)]
    tokens = number * multiplier
    if tokens < 1 or tokens > MAX_CONTEXT_TOKENS:
        raise ValueError(
            "context_size must resolve to between 1 and %s tokens"
            % MAX_CONTEXT_TOKENS
        )
    if tokens != tokens.to_integral_value():
        raise ValueError("context_size must resolve to a whole number of tokens")
    return text


def _output_text(*values):
    chunks = []
    for value in values:
        if not value:
            continue
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        value = str(value).strip()
        if value:
            chunks.append(value)
    output = "\n".join(chunks)
    if len(output) <= MAX_OPERATION_OUTPUT:
        return output
    marker = "[output truncated]\n"
    return marker + output[-(MAX_OPERATION_OUTPUT - len(marker)):]


def _bounded_seconds(value, default, maximum):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = float(default)
    return max(1.0, min(parsed, float(maximum)))


def _retention_limit(value):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = DEFAULT_OPERATION_RETENTION
    return max(1, min(parsed, MAX_OPERATION_RETENTION))


def _pid_alive(pid):
    return _process_pid_alive(pid)


def _windows_process_identity(pid):
    try:
        import ctypes

        class FileTime(ctypes.Structure):
            _fields_ = [
                ("low", ctypes.c_uint32),
                ("high", ctypes.c_uint32),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (
            ctypes.c_ulong,
            ctypes.c_int,
            ctypes.c_ulong,
        )
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetProcessTimes.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(FileTime),
            ctypes.POINTER(FileTime),
            ctypes.POINTER(FileTime),
            ctypes.POINTER(FileTime),
        )
        kernel32.GetProcessTimes.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return ""
        try:
            creation = FileTime()
            exit_time = FileTime()
            kernel = FileTime()
            user = FileTime()
            if not kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return ""
            value = (int(creation.high) << 32) | int(creation.low)
            return "windows:%d" % value
        finally:
            kernel32.CloseHandle(handle)
    except (AttributeError, ImportError, OSError, ValueError):
        return ""


def _linux_proc_path(pid):
    """Resolve a namespace PID even when /proc is mounted from the host."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    try:
        namespace_inode = os.stat("/proc/self/ns/pid").st_ino
        entries = Path("/proc").iterdir()
    except OSError:
        return None
    direct = Path("/proc/%d" % pid)
    candidates = ([direct] if direct.exists() else []) + list(entries)
    seen = set()
    for entry in candidates:
        if not entry.name.isdigit():
            continue
        if entry.name in seen:
            continue
        seen.add(entry.name)
        try:
            if os.stat(entry / "ns/pid").st_ino != namespace_inode:
                continue
            status = (entry / "status").read_text(
                encoding="ascii", errors="replace"
            )
        except OSError:
            continue
        for line in status.splitlines():
            if line.startswith("NSpid:"):
                values = line.split()[1:]
                if values and int(values[-1]) == pid:
                    return entry
                break
    return None


def _linux_process_fields(pid):
    try:
        process_path = _linux_proc_path(pid)
        if process_path is None:
            return None
        raw = (process_path / "stat").read_text(encoding="ascii")
        status = (process_path / "status").read_text(
            encoding="ascii", errors="replace"
        )
        end = raw.rfind(")")
        if end < 0:
            return None
        fields = raw[end + 2:].split()
        if len(fields) < 20:
            return None
        result = {
            "state": fields[0],
            "ppid": int(fields[1]),
            "group": int(fields[2]),
            "session": int(fields[3]),
            "start_ticks": fields[19],
        }
        for line in status.splitlines():
            if line.startswith("NSpgid:"):
                values = line.split()[1:]
                if values:
                    result["group"] = int(values[-1])
            elif line.startswith("NSsid:"):
                values = line.split()[1:]
                if values:
                    result["session"] = int(values[-1])
        return result
    except (OSError, ValueError):
        return None


def _process_start_identity(pid):
    if os.name == "nt":
        return _windows_process_identity(pid)
    fields = _linux_process_fields(pid)
    if fields:
        try:
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(
                encoding="ascii"
            ).strip()
        except OSError:
            boot_id = "unknown-boot"
        return "linux:%s:%s" % (boot_id, fields["start_ticks"])
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(int(pid))],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""
    value = result.stdout.strip()
    return "posix:%s" % value if result.returncode == 0 and value else ""


def _process_matches(pid, identity):
    return bool(identity) and _pid_alive(pid) and _process_start_identity(pid) == identity


def _posix_group_alive(group_id):
    try:
        group_id = int(group_id)
    except (TypeError, ValueError):
        return False
    if group_id <= 1:
        return False
    try:
        os.killpg(group_id, 0)
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True


def _wait_until(predicate, timeout, interval=0.05):
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        if predicate():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(interval, remaining))


class _BoundedOutputTail:
    """Thread-safe byte tail that never grows with command output volume."""

    def __init__(self, limit=MAX_OPERATION_OUTPUT):
        self.limit = max(256, int(limit))
        self.data = bytearray()
        self.truncated = False
        self.lock = threading.Lock()

    def append(self, chunk):
        if not chunk:
            return
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8", errors="replace")
        with self.lock:
            self.data.extend(chunk)
            if len(self.data) > self.limit:
                del self.data[:len(self.data) - self.limit]
                self.truncated = True

    def text(self):
        with self.lock:
            value = bytes(self.data).decode("utf-8", errors="replace")
            truncated = self.truncated
        return ("[output truncated]\n" + value) if truncated else value


def _drain_process_output(pipe, tail):
    try:
        while True:
            chunk = pipe.read(8192)
            if not chunk:
                return
            tail.append(chunk)
    except (OSError, ValueError):
        return


def _windows_process_table():
    if os.name != "nt":
        return {}
    try:
        import ctypes
        from ctypes import wintypes

        class ProcessEntry(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateToolhelp32Snapshot.argtypes = (
            wintypes.DWORD,
            wintypes.DWORD,
        )
        kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        kernel32.Process32FirstW.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(ProcessEntry),
        )
        kernel32.Process32FirstW.restype = wintypes.BOOL
        kernel32.Process32NextW.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(ProcessEntry),
        )
        kernel32.Process32NextW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
        if snapshot == ctypes.c_void_p(-1).value:
            return {}
        try:
            entry = ProcessEntry()
            entry.dwSize = ctypes.sizeof(ProcessEntry)
            processes = {}
            more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while more:
                processes[int(entry.th32ProcessID)] = int(
                    entry.th32ParentProcessID
                )
                more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
            return processes
        finally:
            kernel32.CloseHandle(snapshot)
    except (AttributeError, ImportError, OSError, ValueError):
        return {}


def _windows_descendants(root_pid):
    table = _windows_process_table()
    descendants = set()
    frontier = {int(root_pid)}
    while frontier:
        children = {
            pid for pid, parent in table.items()
            if parent in frontier and pid not in descendants
        }
        descendants.update(children)
        frontier = children
    return descendants


def _windows_terminate_pid(pid, identity=""):
    if identity and not _process_matches(pid, identity):
        return False
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (
            ctypes.c_ulong,
            ctypes.c_int,
            ctypes.c_ulong,
        )
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.TerminateProcess.argtypes = (ctypes.c_void_p, ctypes.c_uint)
        kernel32.TerminateProcess.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.OpenProcess(0x0001 | 0x1000, False, int(pid))
        if not handle:
            return not _pid_alive(pid)
        try:
            if identity and _windows_process_identity(pid) != identity:
                return False
            return bool(kernel32.TerminateProcess(handle, 1))
        finally:
            kernel32.CloseHandle(handle)
    except (AttributeError, ImportError, OSError, ValueError):
        return False


def _normalize_idempotency_key(value):
    key = str(value or "").strip()
    if key and not _IDEMPOTENCY_KEY.fullmatch(key):
        raise ValueError(
            "Idempotency-Key must be 8-128 letters, numbers, or . _ : -"
        )
    return key


def _write_private_file(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    temporary = path.with_name(".%s.%s.tmp" % (path.name, uuid.uuid4().hex))
    descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(value + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _provision_health_token(path, configured=""):
    configured = str(configured or "").strip()
    if configured and len(configured) < sonder_health.MIN_TOKEN_LENGTH:
        raise ValueError(
            "%s must contain at least %s characters"
            % (sonder_health.TOKEN_ENV, sonder_health.MIN_TOKEN_LENGTH)
        )
    token_path = Path(path)
    if token_path.is_symlink():
        raise ValueError("launcher health token path must not be a symbolic link")
    exists = token_path.exists()
    try:
        existing = token_path.read_text(encoding="ascii").strip() if exists else ""
    except (OSError, UnicodeError) as exc:
        raise ValueError("persisted launcher health token could not be read") from exc
    if exists and len(existing) < sonder_health.MIN_TOKEN_LENGTH:
        raise ValueError("persisted launcher health token is invalid")
    if configured:
        if existing and existing != configured:
            raise ValueError(
                "configured launcher health token does not match the persisted token"
            )
        if not existing:
            _write_private_file(token_path, configured)
        return configured
    if len(existing) >= sonder_health.MIN_TOKEN_LENGTH:
        try:
            os.chmod(token_path, 0o600)
        except OSError:
            pass
        return existing
    token = secrets.token_urlsafe(32)
    _write_private_file(token_path, token)
    return token


class LauncherController:
    """Host-controlled launcher with a persistent, single-operation queue."""

    def __init__(
        self,
        root=ROOT,
        python=sys.executable,
        server_host="0.0.0.0",
        server_port=SERVER_PORT,
        *,
        db_path=None,
        health_token_path=None,
        health_token=None,
        start_timeout=None,
        stop_timeout=None,
        retention=None,
    ):
        self.root = Path(root).resolve()
        self.python = str(python)
        self.server_host = str(server_host)
        self.server_port = int(server_port)
        self.db_path = Path(
            db_path
            or state_path(
                "run/sonder-launcher-operations.sqlite3", "SONDER_LAUNCHER_DB"
            )
        ).expanduser().resolve()
        self.health_token_path = Path(
            health_token_path or self.db_path.with_name("sonder-launcher-health.token")
        ).expanduser().absolute()
        configured_health_token = (
            health_token
            if health_token is not None
            else os.environ.get(sonder_health.TOKEN_ENV, "")
        )
        self.start_timeout = _bounded_seconds(
            start_timeout
            if start_timeout is not None
            else os.environ.get("SONDER_LAUNCHER_START_TIMEOUT"),
            START_ACTION_TIMEOUT_SECONDS,
            START_ACTION_TIMEOUT_SECONDS,
        )
        self.stop_timeout = _bounded_seconds(
            stop_timeout
            if stop_timeout is not None
            else os.environ.get("SONDER_LAUNCHER_STOP_TIMEOUT"),
            STOP_ACTION_TIMEOUT_SECONDS,
            STOP_ACTION_TIMEOUT_SECONDS,
        )
        self.retention = _retention_limit(
            retention
            if retention is not None
            else os.environ.get("SONDER_LAUNCHER_OPERATION_RETENTION")
        )
        self._lock = threading.RLock()
        self._threads_lock = threading.Lock()
        self._threads = {}
        self._operation_context = threading.local()
        self.last_action = ""
        self.last_error = ""
        self.last_action_ts = 0
        self._initialize_database()
        self.health_token = self._provision_controller_health_token(
            configured_health_token
        )
        self.recover_interrupted()

    def _connect(self):
        connection = sqlite3.connect(
            str(self.db_path), timeout=5, check_same_thread=False
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _initialize_database(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript(_SCHEMA)
            connection.execute("BEGIN IMMEDIATE")
            columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(sonder_launcher_operations)"
                ).fetchall()
            }
            for name, sql_type in _OPERATION_MIGRATIONS.items():
                if name not in columns:
                    connection.execute(
                        "ALTER TABLE sonder_launcher_operations ADD COLUMN %s %s"
                        % (name, sql_type)
                    )
            connection.commit()
        finally:
            connection.close()
        try:
            os.chmod(self.db_path, 0o600)
        except OSError:
            pass

    def _provision_controller_health_token(self, configured):
        # The SQLite write lock serializes first-run token creation across two
        # launcher processes sharing the per-user state directory.
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            token = _provision_health_token(
                self.health_token_path, configured
            )
            connection.commit()
            return token
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _operation_from_row(row):
        if row is None:
            return None
        try:
            commands = json.loads(row["commands_json"] or "[]")
        except (TypeError, ValueError):
            commands = []
        if not isinstance(commands, list):
            commands = []
        return {
            "id": row["id"],
            "action": row["action"],
            "context_size": row["context_size"],
            "phase": row["phase"],
            "created_ts": row["created_ts"],
            "started_ts": row["started_ts"],
            "updated_ts": row["updated_ts"],
            "finished_ts": row["finished_ts"],
            "message": row["message"] or "",
            "last_error": row["last_error"] or "",
            "command": commands[-1] if commands else [],
            "commands": commands,
        }

    def _worker_key(self, operation_id, owner_id):
        return (str(self.db_path), str(operation_id), str(owner_id))

    def _register_worker(self, operation_id, owner_id, thread=None):
        with _LIVE_WORKERS_LOCK:
            _LIVE_WORKERS[self._worker_key(operation_id, owner_id)] = {
                "thread": thread,
                "registered": time.monotonic(),
            }

    def _unregister_worker(self, operation_id, owner_id):
        with _LIVE_WORKERS_LOCK:
            _LIVE_WORKERS.pop(self._worker_key(operation_id, owner_id), None)

    def _worker_alive(self, operation_id, owner_id):
        with _LIVE_WORKERS_LOCK:
            entry = _LIVE_WORKERS.get(self._worker_key(operation_id, owner_id))
            if not entry:
                return False
            thread = entry.get("thread")
            if thread is None:
                return time.monotonic() - entry["registered"] < 5.0
            return thread.is_alive() or time.monotonic() - entry["registered"] < 1.0

    def _operation_stale(self, lock, operation, now):
        if not lock or not lock.get("operation_id"):
            return False
        deadline = float(
            (operation or {}).get("hard_deadline")
            or lock.get("lease_until")
            or 0
        )
        if deadline and now >= deadline:
            return True
        if lock.get("owner_host") == socket.gethostname():
            owner_pid = int(lock.get("owner_pid") or 0)
            if not _pid_alive(owner_pid):
                return True
            if owner_pid == os.getpid():
                return not self._worker_alive(
                    lock["operation_id"], lock.get("owner_id")
                )
            return False
        return float(lock.get("lease_until") or 0) <= now

    @staticmethod
    def _control_metadata(operation):
        return {
            "pid": int(operation.get("control_pid") or 0),
            "identity": operation.get("control_identity") or "",
            "group": int(operation.get("control_group_id") or 0),
            "platform": operation.get("control_platform") or "",
            "exit_code": operation.get("control_exit_code"),
            "owner_host": operation.get("owner_host") or "",
        }

    def _terminate_control_tree(self, operation, process=None):
        metadata = self._control_metadata(operation)
        pid = metadata["pid"]
        identity = metadata["identity"]
        group_id = metadata["group"]
        if not pid:
            return True

        root_matches = bool(
            process is not None
            and process.pid == pid
            and process.returncode is None
        ) or _process_matches(pid, identity)
        if (
            process is None
            and metadata["exit_code"] is not None
            and not root_matches
        ):
            # Completion is persisted only after successful detach or after a
            # failed/timeout tree was fully reconciled.
            return True
        if (
            metadata["owner_host"]
            and metadata["owner_host"] != socket.gethostname()
        ):
            # Process identifiers and group IDs are host-local. Never signal a
            # coincidentally matching local process for a remote lease owner.
            return False
        if _pid_alive(pid) and not root_matches:
            # PID reuse: never signal a process whose start identity differs.
            return False

        if os.name != "nt":
            if group_id != pid or group_id <= 1 or group_id == os.getpgrp():
                return not root_matches and not _posix_group_alive(group_id)
            if not root_matches and not _posix_group_alive(group_id):
                return True
            try:
                os.killpg(group_id, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except OSError:
                return False
            if process is not None:
                try:
                    process.wait(timeout=CONTROL_TERMINATE_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    pass
            if not _wait_until(
                lambda: not _posix_group_alive(group_id),
                CONTROL_TERMINATE_GRACE_SECONDS,
            ):
                try:
                    os.killpg(group_id, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except OSError:
                    return False
            if process is not None:
                try:
                    process.wait(timeout=CONTROL_KILL_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    return False
            return _wait_until(
                lambda: not _posix_group_alive(group_id),
                CONTROL_KILL_GRACE_SECONDS,
            )

        descendants = _windows_descendants(pid)
        if process is not None and root_matches:
            try:
                process.send_signal(signal.CTRL_BREAK_EVENT)
                process.wait(timeout=CONTROL_TERMINATE_GRACE_SECONDS)
            except (AttributeError, OSError, subprocess.TimeoutExpired):
                pass
        descendants.update(_windows_descendants(pid))
        for child_pid in sorted(descendants, reverse=True):
            child_identity = _process_start_identity(child_pid)
            if child_identity:
                _windows_terminate_pid(child_pid, child_identity)
        if root_matches:
            _windows_terminate_pid(pid, identity)
        if process is not None:
            try:
                process.wait(timeout=CONTROL_KILL_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                return False
        return _wait_until(
            lambda: not _process_matches(pid, identity)
            and not any(
                _pid_alive(child)
                for child in descendants.union(_windows_descendants(pid))
            ),
            CONTROL_KILL_GRACE_SECONDS,
        )

    def _recovery_blocked(self, operation_id, owner_id):
        connection = self._connect()
        try:
            with connection:
                connection.execute(
                    """
                    UPDATE sonder_launcher_operations
                    SET updated_ts=?, last_error=?, message=?
                    WHERE id=? AND owner_id=? AND phase IN ('queued','running')
                    """,
                    (
                        time.time(),
                        "stale control process tree could not be proven stopped",
                        "Recovery is blocked; the operation lock remains held.",
                        operation_id,
                        owner_id,
                    ),
                )
        finally:
            connection.close()

    def _interrupt_recovered_operation(self, lock, operation, reason):
        now = time.time()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current_lock = connection.execute(
                "SELECT * FROM sonder_launcher_operation_lock WHERE id=1"
            ).fetchone()
            current = connection.execute(
                "SELECT * FROM sonder_launcher_operations WHERE id=?",
                (operation["id"],),
            ).fetchone()
            if (
                not current_lock
                or current_lock["operation_id"] != operation["id"]
                or current_lock["owner_id"] != lock.get("owner_id")
                or not current
                or current["owner_id"] != operation.get("owner_id")
                or current["phase"] not in ACTIVE_PHASES
                or current["control_pid"] != operation.get("control_pid")
                or (current["control_identity"] or "")
                != (operation.get("control_identity") or "")
            ):
                connection.rollback()
                return False
            connection.execute(
                """
                UPDATE sonder_launcher_operations
                SET phase='interrupted', updated_ts=?, finished_ts=?,
                    last_error=?, message=? WHERE id=?
                """,
                (
                    now,
                    now,
                    reason,
                    "Operation interrupted; inspect server status before retrying.",
                    operation["id"],
                ),
            )
            connection.execute(
                """
                UPDATE sonder_launcher_operation_lock
                SET operation_id=NULL, owner_id=NULL, owner_pid=NULL,
                    owner_host=NULL, lease_until=NULL
                WHERE id=1 AND operation_id=? AND owner_id=?
                """,
                (operation["id"], lock.get("owner_id")),
            )
            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def recover_interrupted(self):
        """Reconcile stale control work before releasing its singleton lock."""
        connection = self._connect()
        try:
            lock_row = connection.execute(
                "SELECT * FROM sonder_launcher_operation_lock WHERE id=1"
            ).fetchone()
            lock = dict(lock_row) if lock_row else {}
            operation_row = None
            if lock.get("operation_id"):
                operation_row = connection.execute(
                    "SELECT * FROM sonder_launcher_operations WHERE id=?",
                    (lock["operation_id"],),
                ).fetchone()
            operation = dict(operation_row) if operation_row else None
        finally:
            connection.close()

        if not lock.get("operation_id"):
            return
        if operation is None:
            # Missing ownership metadata is corruption, not proof that no child
            # exists. Fail closed and retain the lock for manual inspection.
            return
        if operation["phase"] not in ACTIVE_PHASES:
            if not self._terminate_control_tree(operation):
                return
            connection = self._connect()
            try:
                with connection:
                    connection.execute(
                        """
                        UPDATE sonder_launcher_operation_lock
                        SET operation_id=NULL, owner_id=NULL, owner_pid=NULL,
                            owner_host=NULL, lease_until=NULL
                        WHERE id=1 AND operation_id=? AND owner_id=?
                        """,
                        (operation["id"], lock.get("owner_id")),
                    )
            finally:
                connection.close()
            return
        if not self._operation_stale(lock, operation, time.time()):
            return
        if not self._terminate_control_tree(operation):
            self._recovery_blocked(operation["id"], operation["owner_id"])
            return
        self._interrupt_recovered_operation(
            lock,
            operation,
            "launcher owner or worker stopped before the operation completed",
        )

    @property
    def command_base(self):
        return [self.python, str(self.root / "sonder_headless.py")]

    def _current_operation_context(self):
        operation_id = getattr(self._operation_context, "operation_id", "")
        owner_id = getattr(self._operation_context, "owner_id", "")
        return (operation_id, owner_id) if operation_id and owner_id else None

    def _persist_control_started(self, pid, identity, group_id, platform_name):
        context = self._current_operation_context()
        if context is None:
            return
        operation_id, owner_id = context
        last_error = None
        for delay in FINALIZE_RETRY_DELAYS:
            if delay:
                time.sleep(delay)
            connection = None
            try:
                connection = self._connect()
                connection.execute("BEGIN IMMEDIATE")
                updated = connection.execute(
                    """
                    UPDATE sonder_launcher_operations
                    SET control_pid=?, control_identity=?, control_group_id=?,
                        control_platform=?, control_started_ts=?,
                        control_finished_ts=NULL, control_exit_code=NULL,
                        updated_ts=?
                    WHERE id=? AND owner_id=? AND phase='running'
                      AND EXISTS (
                          SELECT 1 FROM sonder_launcher_operation_lock
                          WHERE id=1 AND operation_id=? AND owner_id=?
                      )
                    """,
                    (
                        int(pid),
                        identity,
                        int(group_id),
                        platform_name,
                        time.time(),
                        time.time(),
                        operation_id,
                        owner_id,
                        operation_id,
                        owner_id,
                    ),
                ).rowcount
                if updated != 1:
                    connection.rollback()
                    raise LauncherConflictError(
                        "launcher operation lost ownership before command start"
                    )
                connection.commit()
                return
            except LauncherConflictError:
                raise
            except sqlite3.Error as exc:
                if connection is not None:
                    connection.rollback()
                last_error = exc
            finally:
                if connection is not None:
                    connection.close()
        raise last_error or sqlite3.OperationalError(
            "control process metadata could not be persisted"
        )

    def _persist_control_finished(self, pid, identity, return_code):
        context = self._current_operation_context()
        if context is None:
            return
        operation_id, owner_id = context
        last_error = None
        for delay in FINALIZE_RETRY_DELAYS:
            if delay:
                time.sleep(delay)
            connection = None
            try:
                connection = self._connect()
                connection.execute("BEGIN IMMEDIATE")
                updated = connection.execute(
                    """
                    UPDATE sonder_launcher_operations
                    SET control_finished_ts=?, control_exit_code=?, updated_ts=?
                    WHERE id=? AND owner_id=? AND phase='running'
                      AND control_pid=? AND control_identity=?
                      AND EXISTS (
                          SELECT 1 FROM sonder_launcher_operation_lock
                          WHERE id=1 AND operation_id=? AND owner_id=?
                      )
                    """,
                    (
                        time.time(),
                        int(return_code),
                        time.time(),
                        operation_id,
                        owner_id,
                        int(pid),
                        identity,
                        operation_id,
                        owner_id,
                    ),
                ).rowcount
                if updated != 1:
                    connection.rollback()
                    raise LauncherConflictError(
                        "launcher operation lost ownership before command completion"
                    )
                connection.commit()
                return
            except LauncherConflictError:
                raise
            except sqlite3.Error as exc:
                if connection is not None:
                    connection.rollback()
                last_error = exc
            finally:
                if connection is not None:
                    connection.close()
        raise last_error or sqlite3.OperationalError(
            "control process completion could not be persisted"
        )

    @staticmethod
    def _control_popen_kwargs():
        kwargs = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "bufsize": 0,
            "close_fds": os.name != "nt",
        }
        if os.name == "nt":
            kwargs["creationflags"] = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
        else:
            kwargs["start_new_session"] = True
        return kwargs

    def _run(self, action, context_size="8192", timeout=ACTION_TIMEOUT_SECONDS):
        if action not in {"start", "stop", "restart", "status"}:
            raise ValueError("unsupported launcher action")
        command = [*self.command_base, action]
        command.extend([
            "--host", self.server_host,
            "--port", str(self.server_port),
        ])
        if action in {"start", "restart"}:
            command.extend(["--context-size", normalize_context_size(context_size)])
        env = os.environ.copy()
        # Controller configuration is authoritative; stale parent variables must
        # not redirect the child to a different interface or port.
        env["SONDER_HOST"] = self.server_host
        env["SONDER_PORT"] = str(self.server_port)
        env[sonder_health.TOKEN_ENV] = self.health_token
        env[sonder_health.ROLE_ENV] = sonder_health.MANAGED_ROLE
        env[CONTROL_GATE_ENV] = "1"
        try:
            process = subprocess.Popen(
                command,
                cwd=self.root,
                env=env,
                **self._control_popen_kwargs(),
            )
        except OSError as exc:
            return 126, "launcher command could not start: %s" % exc, command

        identity = _process_start_identity(process.pid)
        # Both start_new_session and CREATE_NEW_PROCESS_GROUP establish a group
        # whose identifier is the freshly created control PID.
        group_id = process.pid
        platform_name = "windows-process-group" if os.name == "nt" else "posix-session"
        metadata = {
            "control_pid": process.pid,
            "control_identity": identity,
            "control_group_id": group_id,
            "control_platform": platform_name,
            "control_exit_code": None,
        }
        tail = _BoundedOutputTail()
        try:
            reader = threading.Thread(
                target=_drain_process_output,
                args=(process.stdout, tail),
                name="sonder-launcher-output-%s" % process.pid,
                daemon=True,
            )
            reader.start()
        except Exception as exc:
            if not self._terminate_control_tree(metadata, process=process):
                raise ControlTreeNotStopped(
                    "output reader failed and the command tree survived"
                ) from exc
            return 126, "launcher output reader could not start: %s" % exc, command

        if not identity or not group_id:
            stopped = self._terminate_control_tree(metadata, process=process)
            if not stopped:
                raise ControlTreeNotStopped(
                    "control command identity was unavailable and its tree survived"
                )
            reader.join(timeout=1)
            return 126, "launcher could not establish command ownership", command
        try:
            self._persist_control_started(
                process.pid, identity, group_id, platform_name
            )
        except Exception as exc:
            stopped = self._terminate_control_tree(metadata, process=process)
            if not stopped:
                raise ControlTreeNotStopped(
                    "command ownership could not be persisted and its tree survived"
                ) from exc
            reader.join(timeout=1)
            return 126, "launcher command ownership could not be persisted: %s" % exc, command
        try:
            process.stdin.write(b"\x01")
            process.stdin.flush()
            process.stdin.close()
        except (BrokenPipeError, OSError, ValueError) as exc:
            stopped = self._terminate_control_tree(metadata, process=process)
            if not stopped:
                raise ControlTreeNotStopped(
                    "control gate failed and the command tree survived"
                ) from exc
            reader.join(timeout=1)
            return 126, "launcher control gate could not be released: %s" % exc, command

        timed_out = False
        try:
            return_code = process.wait(timeout=max(0.1, float(timeout)))
        except subprocess.TimeoutExpired:
            timed_out = True
            if not self._terminate_control_tree(metadata, process=process):
                raise ControlTreeNotStopped(
                    "launcher command timed out and its process tree survived"
                )
            return_code = process.returncode
            if return_code is None:
                try:
                    return_code = process.wait(timeout=CONTROL_KILL_GRACE_SECONDS)
                except subprocess.TimeoutExpired as exc:
                    raise ControlTreeNotStopped(
                        "launcher command tree could not be reaped"
                    ) from exc

        metadata["control_exit_code"] = return_code
        if not timed_out and return_code != 0:
            if not self._terminate_control_tree(metadata, process=process):
                raise ControlTreeNotStopped(
                    "failed launcher command left a surviving process tree"
                )

        reader.join(timeout=1)
        output = _output_text(tail.text())
        try:
            self._persist_control_finished(process.pid, identity, return_code)
        except LauncherConflictError as exc:
            raise ControlTreeNotStopped(
                "launcher operation lost ownership after its command exited"
            ) from exc
        except sqlite3.Error as exc:
            return 125, _output_text(
                output, "launcher could not persist command completion: %s" % exc
            ), command
        if timed_out:
            detail = "launcher command timed out after %.1f seconds" % max(
                0.1, float(timeout)
            )
            return 124, _output_text(output, detail), command
        return int(return_code), output, command

    def _server_state(self):
        """Return healthy, stopped, or foreign_listener for the managed port."""
        if not _reachable("127.0.0.1", self.server_port):
            return "stopped"
        nonce = sonder_health.new_nonce()
        request = urllib.request.Request(
            "http://127.0.0.1:%s%s" % (self.server_port, sonder_health.PATH),
            headers={sonder_health.NONCE_HEADER: nonce},
            method="GET",
        )
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(request, timeout=0.8) as response:
                if response.status != 200:
                    return "foreign_listener"
                raw = response.read(4097)
        except (OSError, urllib.error.URLError, ValueError):
            return "foreign_listener"
        if len(raw) > 4096:
            return "foreign_listener"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return "foreign_listener"
        return (
            "healthy"
            if sonder_health.payload_matches(
                payload,
                token=self.health_token,
                nonce=nonce,
                port=self.server_port,
            )
            else "foreign_listener"
        )

    def _wait_for_state(self, running, deadline):
        while True:
            state = self._server_state()
            if (state == "healthy") == bool(running) and (
                running or state == "stopped"
            ):
                return True
            if state == "foreign_listener":
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(POLL_INTERVAL_SECONDS, remaining))

    def _latest_operations(self):
        connection = self._connect()
        try:
            active = connection.execute(
                """
                SELECT * FROM sonder_launcher_operations
                WHERE phase IN ('queued','running')
                ORDER BY created_ts DESC LIMIT 1
                """
            ).fetchone()
            latest = connection.execute(
                "SELECT * FROM sonder_launcher_operations ORDER BY created_ts DESC LIMIT 1"
            ).fetchone()
            return self._operation_from_row(active), self._operation_from_row(latest)
        finally:
            connection.close()

    def status(self):
        self.recover_interrupted()
        active, latest = self._latest_operations()
        server_state = self._server_state()
        running = server_state == "healthy"
        persisted = latest if latest and latest["created_ts"] >= self.last_action_ts else None
        return {
            "ok": True,
            "launcher": "ready",
            "server_running": running,
            "server_state": server_state,
            "server_role": (
                sonder_health.MANAGED_ROLE if running else ""
            ),
            "server_host": self.server_host,
            "server_port": self.server_port,
            "last_action": persisted["action"] if persisted else self.last_action,
            "last_action_ts": (
                int(persisted["created_ts"]) if persisted else self.last_action_ts
            ),
            "last_error": persisted["last_error"] if persisted else self.last_error,
            "active_operation": active,
        }

    def operation(self, operation_id):
        operation_id = str(operation_id or "").strip().lower()
        if not _OPERATION_ID.fullmatch(operation_id):
            return None
        self.recover_interrupted()
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM sonder_launcher_operations WHERE id=?", (operation_id,)
            ).fetchone()
            return self._operation_from_row(row)
        finally:
            connection.close()

    def operation_payload(self, operation):
        if not operation:
            return None
        payload = self.status()
        phase = operation["phase"]
        payload.update({
            "ok": phase not in {"failed", "cancelled", "interrupted"},
            "operation_id": operation["id"],
            "operation_phase": phase,
            "operation": operation,
            "message": operation["message"],
            "last_error": operation["last_error"],
            "command": operation["command"],
            "commands": operation["commands"],
        })
        return payload

    def _timeout_for_action(self, action):
        return self.stop_timeout if action == "stop" else self.start_timeout

    def submit(self, action, context_size="8192", idempotency_key=""):
        if action not in {"start", "stop", "restart"}:
            raise ValueError("unsupported launcher action")
        context_size = normalize_context_size(context_size)
        idempotency_key = _normalize_idempotency_key(idempotency_key)
        self.recover_interrupted()
        now = time.time()
        operation_id = uuid.uuid4().hex
        owner_id = uuid.uuid4().hex
        timeout = self._timeout_for_action(action)
        lease_until = now + timeout + LOCK_GRACE_SECONDS
        hard_deadline = lease_until
        connection = self._connect()
        created = False
        worker_registered = False
        try:
            connection.execute("BEGIN IMMEDIATE")
            if idempotency_key:
                existing = connection.execute(
                    "SELECT * FROM sonder_launcher_operations WHERE idempotency_key=?",
                    (idempotency_key,),
                ).fetchone()
                if existing:
                    operation = self._operation_from_row(existing)
                    if (
                        operation["action"] != action
                        or operation["context_size"] != context_size
                    ):
                        raise LauncherConflictError(
                            "Idempotency-Key was already used for a different request",
                            operation,
                        )
                    connection.commit()
                    return operation, False

            lock = connection.execute(
                "SELECT * FROM sonder_launcher_operation_lock WHERE id=1"
            ).fetchone()
            if lock and lock["operation_id"]:
                active = connection.execute(
                    "SELECT * FROM sonder_launcher_operations WHERE id=?",
                    (lock["operation_id"],),
                ).fetchone()
                raise LauncherConflictError(
                    "another launcher operation is already active",
                    self._operation_from_row(active),
                )

            connection.execute(
                """
                INSERT INTO sonder_launcher_operations(
                    id,action,context_size,phase,idempotency_key,created_ts,
                    updated_ts,owner_id,owner_pid,owner_host,lease_until,
                    hard_deadline
                ) VALUES(?,?,?,'queued',?,?,?,?,?,?,?,?)
                """,
                (
                    operation_id,
                    action,
                    context_size,
                    idempotency_key or None,
                    now,
                    now,
                    owner_id,
                    os.getpid(),
                    socket.gethostname(),
                    lease_until,
                    hard_deadline,
                ),
            )
            connection.execute(
                """
                UPDATE sonder_launcher_operation_lock
                SET operation_id=?, owner_id=?, owner_pid=?, owner_host=?,
                    lease_until=? WHERE id=1
                """,
                (
                    operation_id,
                    owner_id,
                    os.getpid(),
                    socket.gethostname(),
                    lease_until,
                ),
            )
            row = connection.execute(
                "SELECT * FROM sonder_launcher_operations WHERE id=?", (operation_id,)
            ).fetchone()
            self._register_worker(operation_id, owner_id)
            worker_registered = True
            connection.commit()
            created = True
        except Exception:
            connection.rollback()
            if worker_registered:
                self._unregister_worker(operation_id, owner_id)
            raise
        finally:
            connection.close()

        operation = self._operation_from_row(row)
        if created:
            try:
                thread = threading.Thread(
                    target=self._operation_worker,
                    args=(operation_id, owner_id, action, context_size, timeout),
                    name="sonder-launcher-%s" % operation_id[:8],
                    daemon=True,
                )
                with self._threads_lock:
                    self._threads[operation_id] = thread
                self._register_worker(operation_id, owner_id, thread)
                thread.start()
            except Exception as exc:
                with self._threads_lock:
                    self._threads.pop(operation_id, None)
                self._finish_operation_with_retry(
                    operation_id,
                    owner_id,
                    "interrupted",
                    "launcher worker could not start: %s" % exc,
                    "launcher worker could not start: %s" % exc,
                    [],
                )
                self._unregister_worker(operation_id, owner_id)
                raise
        return operation, True

    def _operation_worker(
        self, operation_id, owner_id, action, context_size, timeout
    ):
        try:
            now = time.time()
            connection = self._connect()
            try:
                with connection:
                    updated = connection.execute(
                        """
                        UPDATE sonder_launcher_operations
                        SET phase='running', started_ts=?, updated_ts=?
                        WHERE id=? AND owner_id=? AND phase='queued'
                          AND EXISTS (
                              SELECT 1 FROM sonder_launcher_operation_lock
                              WHERE id=1 AND operation_id=? AND owner_id=?
                          )
                        """,
                        (
                            now,
                            now,
                            operation_id,
                            owner_id,
                            operation_id,
                            owner_id,
                        ),
                    ).rowcount
            finally:
                connection.close()
            if updated != 1:
                return
            self._operation_context.operation_id = operation_id
            self._operation_context.owner_id = owner_id
            try:
                result = self.action(action, context_size, timeout=timeout)
                phase = "succeeded" if result.get("ok") else "failed"
                message = result.get("message", "")
                last_error = result.get("last_error", "")
                commands = result.get("commands", [])
            except ControlTreeNotStopped:
                # Retain the active row and lock. Recovery may only release them
                # after it proves the persisted control tree has stopped.
                return
            except Exception as exc:
                phase = "failed"
                message = "launcher operation failed: %s" % exc
                last_error = message
                commands = []
            self._finish_operation_with_retry(
                operation_id,
                owner_id,
                phase,
                message,
                last_error,
                commands,
            )
        finally:
            self._operation_context.operation_id = ""
            self._operation_context.owner_id = ""
            self._unregister_worker(operation_id, owner_id)
            with self._threads_lock:
                self._threads.pop(operation_id, None)

    def _finish_operation_with_retry(
        self, operation_id, owner_id, phase, message, last_error, commands
    ):
        error = None
        for delay in FINALIZE_RETRY_DELAYS:
            if delay:
                time.sleep(delay)
            try:
                return self._finish_operation(
                    operation_id,
                    owner_id,
                    phase,
                    message,
                    last_error,
                    commands,
                )
            except (OSError, sqlite3.Error) as exc:
                error = exc
        try:
            return self._emergency_finish_operation(
                operation_id,
                owner_id,
                phase,
                message,
                last_error or ("finalization retry failed: %s" % error),
                commands,
            )
        except (OSError, sqlite3.Error):
            return False

    def _emergency_finish_operation(
        self, operation_id, owner_id, phase, message, last_error, commands
    ):
        """Minimal independent terminal transaction after normal retries fail."""
        now = time.time()
        commands_json = json.dumps(
            commands if isinstance(commands, list) else [], separators=(",", ":")
        )
        if len(commands_json) > MAX_OPERATION_OUTPUT:
            commands_json = "[]"
        connection = sqlite3.connect(str(self.db_path), timeout=30)
        try:
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE sonder_launcher_operations
                SET phase=?, updated_ts=?, finished_ts=?, message=?,
                    last_error=?, commands_json=?
                WHERE id=? AND owner_id=? AND phase IN ('queued','running')
                  AND EXISTS (
                      SELECT 1 FROM sonder_launcher_operation_lock
                      WHERE id=1 AND operation_id=? AND owner_id=?
                  )
                """,
                (
                    phase,
                    now,
                    now,
                    _output_text(message),
                    _output_text(last_error),
                    commands_json,
                    operation_id,
                    owner_id,
                    operation_id,
                    owner_id,
                ),
            ).rowcount
            if updated == 1:
                connection.execute(
                    """
                    UPDATE sonder_launcher_operation_lock
                    SET operation_id=NULL, owner_id=NULL, owner_pid=NULL,
                        owner_host=NULL, lease_until=NULL
                    WHERE id=1 AND operation_id=? AND owner_id=?
                    """,
                    (operation_id, owner_id),
                )
            connection.commit()
            return updated == 1
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _finish_operation(
        self, operation_id, owner_id, phase, message, last_error, commands
    ):
        if phase not in TERMINAL_PHASES:
            raise ValueError("invalid terminal launcher phase")
        now = time.time()
        safe_commands = commands if isinstance(commands, list) else []
        commands_json = json.dumps(safe_commands, separators=(",", ":"))
        if len(commands_json) > MAX_OPERATION_OUTPUT:
            safe_commands = safe_commands[-2:]
            commands_json = json.dumps(safe_commands, separators=(",", ":"))[
                :MAX_OPERATION_OUTPUT
            ]
            try:
                json.loads(commands_json)
            except ValueError:
                commands_json = "[]"
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE sonder_launcher_operations
                SET phase=?, updated_ts=?, finished_ts=?, message=?,
                    last_error=?, commands_json=?
                WHERE id=? AND owner_id=? AND phase IN ('queued','running')
                  AND EXISTS (
                      SELECT 1 FROM sonder_launcher_operation_lock
                      WHERE id=1 AND operation_id=? AND owner_id=?
                  )
                """,
                (
                    phase,
                    now,
                    now,
                    _output_text(message),
                    _output_text(last_error),
                    commands_json,
                    operation_id,
                    owner_id,
                    operation_id,
                    owner_id,
                ),
            ).rowcount
            if updated == 1:
                connection.execute(
                    """
                    UPDATE sonder_launcher_operation_lock
                    SET operation_id=NULL, owner_id=NULL, owner_pid=NULL,
                        owner_host=NULL, lease_until=NULL
                    WHERE id=1 AND operation_id=? AND owner_id=?
                    """,
                    (operation_id, owner_id),
                )
            connection.execute(
                """
                DELETE FROM sonder_launcher_operations WHERE id IN (
                    SELECT id FROM sonder_launcher_operations
                    WHERE phase IN ('succeeded','failed','cancelled','interrupted')
                    ORDER BY created_ts DESC LIMIT -1 OFFSET ?
                )
                """,
                (self.retention,),
            )
            connection.commit()
            return updated == 1
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def wait_operation(self, operation_id, timeout=5):
        deadline = time.monotonic() + max(0, float(timeout))
        while True:
            operation = self.operation(operation_id)
            if not operation or operation["phase"] in TERMINAL_PHASES:
                return operation
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return operation
            time.sleep(min(0.02, remaining))

    def action(self, action, context_size="8192", *, timeout=None):
        with self._lock:
            if action not in {"start", "stop", "restart"}:
                raise ValueError("unsupported launcher action")
            context_size = normalize_context_size(context_size)
            initial_state = self._server_state()
            if initial_state == "foreign_listener":
                failure = (
                    "the configured port is occupied by an unverified listener; "
                    "refusing to run launcher commands"
                )
                self.last_action = action
                self.last_action_ts = int(time.time())
                self.last_error = failure
                return {
                    **self.status(),
                    "ok": False,
                    "message": failure,
                    "last_error": failure,
                    "command": [],
                    "commands": [],
                }
            if action == "start" and initial_state == "healthy":
                return {
                    **self.status(),
                    "message": "Sonder server is already running.",
                }

            self.last_action = action
            self.last_action_ts = int(time.time())
            total_timeout = _bounded_seconds(
                timeout,
                self._timeout_for_action(action),
                self._timeout_for_action(action),
            )
            deadline = time.monotonic() + total_timeout
            steps = ("stop", "start") if action == "restart" else (action,)
            outputs = []
            commands = []
            failure = ""
            for step in steps:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    failure = "launcher action timed out before %s could run" % step
                    break
                step_timeout = min(
                    remaining,
                    self.stop_timeout if step == "stop" else self.start_timeout,
                )
                step_deadline = min(deadline, time.monotonic() + step_timeout)
                code, output, command = self._run(
                    step, context_size, step_timeout
                )
                commands.append(
                    [Path(command[0]).name, Path(command[1]).name, *command[2:]]
                )
                if output:
                    outputs.append("%s: %s" % (step, output))
                if code != 0:
                    failure = output or "%s command failed with exit code %s" % (step, code)
                    break
                expected_running = step == "start"
                if not self._wait_for_state(expected_running, step_deadline):
                    current_state = self._server_state()
                    if current_state == "foreign_listener":
                        failure = (
                            "the configured port is occupied by an unverified listener"
                        )
                    else:
                        failure = (
                            "server did not become healthy before the deadline"
                            if expected_running
                            else "server remained healthy after the stop request"
                        )
                    break

            payload = self.status()
            expected_running = action != "stop"
            if not failure and payload["server_running"] != expected_running:
                failure = (
                    "server is not reachable after the %s request" % action
                    if expected_running
                    else "server is still reachable after the stop request"
                )
            self.last_error = failure
            payload["last_error"] = failure
            failure_is_reported = any(failure and failure in output for output in outputs)
            message_parts = outputs + (
                [failure] if failure and not failure_is_reported else []
            )
            payload.update({
                "ok": not failure,
                "message": _output_text(*message_parts) or "%s completed" % action,
                "command": commands[-1] if commands else [],
                "commands": commands,
            })
            return payload


class LauncherServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, *, controller, token):
        super().__init__(address, handler)
        self.controller = controller
        self.token = token


class LauncherHandler(BaseHTTPRequestHandler):
    server_version = "SonderLauncher/1"

    def log_message(self, fmt, *args):
        if os.environ.get("SONDER_LAUNCHER_QUIET") != "1":
            super().log_message(fmt, *args)

    def _authorized(self):
        expected = self.server.token
        if not expected:
            return _loopback(self.client_address[0])
        supplied = self.headers.get("Authorization", "")
        if supplied.lower().startswith("bearer "):
            supplied = supplied[7:].strip()
        else:
            supplied = self.headers.get("X-Sonder-Launcher-Token", "").strip()
        return bool(supplied) and hmac.compare_digest(supplied, expected)

    def _send(self, payload, status=200, headers=None):
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self):
        if self._authorized():
            return True
        self._send({"ok": False, "error": "launcher authentication required"}, 401)
        return False

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        is_status = path in {"", "/v1/launcher/status"}
        operation_id = ""
        operation_prefix = "/v1/launcher/operations/"
        if path.startswith(operation_prefix):
            suffix = path[len(operation_prefix):]
            if "/" not in suffix:
                operation_id = suffix.lower()
        if not is_status and not _OPERATION_ID.fullmatch(operation_id):
            self._send({"ok": False, "error": "not found"}, 404)
            return
        if not self._auth():
            return
        if is_status:
            self._send(self.server.controller.status())
            return
        operation = self.server.controller.operation(operation_id)
        if not operation:
            self._send({"ok": False, "error": "operation not found"}, 404)
            return
        self._send(self.server.controller.operation_payload(operation))

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        action = path.rsplit("/", 1)[-1]
        if path not in {
            "/v1/launcher/start", "/v1/launcher/stop", "/v1/launcher/restart",
        }:
            self._send({"ok": False, "error": "not found"}, 404)
            return
        if not self._auth():
            return
        if self.headers.get("Transfer-Encoding"):
            self._send({"ok": False, "error": "transfer encoding is not supported"}, 400)
            return
        content_type = self.headers.get("Content-Type", "")
        if content_type.split(";", 1)[0].strip().lower() != "application/json":
            self._send({"ok": False, "error": "Content-Type must be application/json"}, 415)
            return
        raw_length = self.headers.get("Content-Length")
        if raw_length is None or not raw_length.isdigit():
            self._send({"ok": False, "error": "valid Content-Length is required"}, 411)
            return
        try:
            length = int(raw_length)
        except ValueError:
            self._send({"ok": False, "error": "invalid content length"}, 400)
            return
        if length < 0 or length > MAX_BODY:
            self._send({"ok": False, "error": "request too large"}, 413)
            return
        previous_timeout = self.connection.gettimeout()
        try:
            self.connection.settimeout(BODY_READ_TIMEOUT_SECONDS)
            raw_body = self.rfile.read(length)
        except (OSError, TimeoutError):
            self._send({"ok": False, "error": "request body is incomplete"}, 400)
            return
        finally:
            try:
                self.connection.settimeout(previous_timeout)
            except OSError:
                pass
        if len(raw_body) != length:
            self._send({"ok": False, "error": "request body is incomplete"}, 400)
            return
        try:
            body = json.loads(raw_body or b"{}")
        except (ValueError, UnicodeDecodeError):
            self._send({"ok": False, "error": "invalid JSON"}, 400)
            return
        if not isinstance(body, dict):
            self._send({"ok": False, "error": "JSON body must be an object"}, 400)
            return
        unexpected = sorted(set(body) - {"context_size"})
        if unexpected:
            self._send(
                {
                    "ok": False,
                    "error": "unsupported request field(s): %s"
                    % ", ".join(unexpected),
                },
                400,
            )
            return
        try:
            context_size = normalize_context_size(body.get("context_size") or "8192")
            idempotency_key = _normalize_idempotency_key(
                self.headers.get("Idempotency-Key", "")
            )
        except ValueError as exc:
            self._send({"ok": False, "error": str(exc)}, 400)
            return
        try:
            operation, created = self.server.controller.submit(
                action, context_size, idempotency_key
            )
        except LauncherConflictError as exc:
            payload = self.server.controller.status()
            payload.update({"ok": False, "error": str(exc)})
            if exc.operation:
                payload.update({
                    "operation_id": exc.operation["id"],
                    "operation_phase": exc.operation["phase"],
                    "operation": exc.operation,
                })
            self._send(payload, 409)
            return
        except (OSError, RuntimeError, sqlite3.Error) as exc:
            self._send(
                {"ok": False, "error": "launcher operation could not be queued: %s" % exc},
                503,
            )
            return
        payload = self.server.controller.operation_payload(operation)
        payload.update({
            "accepted": created,
            "idempotent_replay": not created,
        })
        terminal = operation["phase"] in TERMINAL_PHASES
        self._send(
            payload,
            200 if terminal else 202,
            {"Location": "/v1/launcher/operations/%s" % operation["id"]},
        )


def generate_token():
    return secrets.token_urlsafe(32)


def validate_configuration(host, token):
    if not _loopback(host) and len(token) < 24:
        raise ValueError("LAN launcher binding requires SONDER_LAUNCHER_TOKEN with at least 24 characters")


def serve(host, port, token, controller=None, cert="", key=""):
    validate_configuration(host, token)
    server = LauncherServer(
        (host, int(port)), LauncherHandler,
        controller=controller or LauncherController(), token=token,
    )
    if cert or key:
        if not cert or not key:
            raise ValueError("both TLS certificate and key are required")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    print("Sonder launcher listening on %s://%s:%s" % ("https" if cert else "http", host, port))
    server.serve_forever()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("SONDER_LAUNCHER_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SONDER_LAUNCHER_PORT", DEFAULT_PORT)))
    parser.add_argument("--token", default=os.environ.get("SONDER_LAUNCHER_TOKEN", ""))
    parser.add_argument("--server-host", default=os.environ.get("SONDER_HOST", "0.0.0.0"))
    parser.add_argument("--server-port", type=int, default=int(os.environ.get("SONDER_PORT", SERVER_PORT)))
    parser.add_argument("--cert", default=os.environ.get("SONDER_LAUNCHER_CERT", ""))
    parser.add_argument("--key", default=os.environ.get("SONDER_LAUNCHER_KEY", ""))
    parser.add_argument("--generate-token", action="store_true")
    args = parser.parse_args(argv)
    if args.generate_token:
        print(generate_token())
        return 0
    try:
        controller = LauncherController(
            server_host=args.server_host, server_port=args.server_port
        )
        serve(args.host, args.port, args.token, controller, args.cert, args.key)
    except (OSError, ValueError, sqlite3.Error) as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
