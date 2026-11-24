"""Minimal authenticated supervisor for starting Sonder from mobile apps.

This process is intentionally independent from server.py and exposes only
status/start/stop/restart. It is not a shell and accepts no executable paths or
arbitrary arguments from clients.
"""
from __future__ import annotations

import sys as _sonder_sys
from pathlib import Path as _SonderPath
_SONDER_REPO_ROOT = _SonderPath(__file__).resolve().parents[1]
for _sonder_p in (str(_SONDER_REPO_ROOT / "scripts"), str(_SONDER_REPO_ROOT / "runtime"), str(_SONDER_REPO_ROOT)):
    if _sonder_p not in _sonder_sys.path:
        _sonder_sys.path.insert(0, _sonder_p)
import argparse
import json
import os
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
from http.server import ThreadingHTTPServer
from pathlib import Path

from sonder_runtime.domain import launcher_health as sonder_health
import command_recovery
from sonder_runtime.adapters.launcher import (
    process_inspection as _process_inspection,
    state_store as _state_store,
)
from sonder_runtime.adapters.launcher.http_handler import (
    LauncherHTTPHandler,
    generate_token,
    unauthenticated_loopback_warning,
)
from sonder_runtime.adapters.launcher.state_store import (
    ACTIVE_PHASES,
    ControlTreeNotStopped,
    LauncherConflictError,
    OPERATION_ID_PATTERN as _OPERATION_ID,
    OPERATION_MIGRATIONS as _OPERATION_MIGRATIONS,
    SCHEMA as _SCHEMA,
    TERMINAL_PHASES,
)
from sonder_runtime.adapters.process_liveness import pid_alive as _process_pid_alive
from sonder_runtime.adapters.launcher_idempotency import (
    _IDEMPOTENCY_KEY,
    _REPLAY_HEADER_NAME,
    _REPLAY_HEADER_VALUE,
    normalize_idempotency_key,
    valid_command_replay,
)
from sonder_runtime.adapters.launcher_probe import probe_error_state, settle_probe
from sonder_runtime.adapters.launcher_output import (
    BoundedOutputTail,
    bounded_seconds,
    drain_process_output,
    output_text,
    retention_limit,
)
from sonder_runtime.application.lifecycle import (
    MAX_CONTEXT_TOKENS,
    _CONTEXT_SIZE,
    normalize_context_size,
)
from sonder_runtime.platform.paths import state_path
from sonder_runtime.platform.runtime_threads import run_bounded
from sonder_runtime.adapters.web import deferred_tls
from sonder_runtime.interfaces.http.connection_limit import BoundedConnectionsMixin
from sonder_runtime.interfaces.http.host_policy import (
    HOST_NOT_ALLOWED_REMEDY,
    host_decision,
    machine_host_names,
    normalize_allowed_host,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 11436
DEFAULT_SERVER_HOST = "127.0.0.1"
SERVER_PORT = 11435
MAX_BODY = 16_384
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
# Compatibility aliases for the historical private policy surface.
_normalize_idempotency_key = normalize_idempotency_key
_valid_command_replay = valid_command_replay
_output_text = output_text
_bounded_seconds = bounded_seconds
_retention_limit = retention_limit
_BoundedOutputTail = BoundedOutputTail
_drain_process_output = drain_process_output
# Preserve module-level re-exports referenced by callers/tests.
_CONTEXT_SIZE = _CONTEXT_SIZE
_IDEMPOTENCY_KEY = _IDEMPOTENCY_KEY
_REPLAY_HEADER_NAME = _REPLAY_HEADER_NAME
_REPLAY_HEADER_VALUE = _REPLAY_HEADER_VALUE
MAX_CONTEXT_TOKENS = MAX_CONTEXT_TOKENS
ACTIVE_PHASES = ACTIVE_PHASES
TERMINAL_PHASES = TERMINAL_PHASES
_SCHEMA = _SCHEMA
_OPERATION_MIGRATIONS = _OPERATION_MIGRATIONS

_LIVE_WORKERS_LOCK = threading.Lock()
_LIVE_WORKERS = {}


def _loopback(host):
    return _process_inspection.is_loopback(host)


def _reachable(host="127.0.0.1", port=SERVER_PORT, timeout=0.4):
    return _process_inspection.is_reachable(host, port, timeout)


def _pid_alive(pid):
    return _process_pid_alive(pid)


def _windows_process_identity(pid):
    return _process_inspection.windows_process_identity(pid)


def _linux_proc_path(pid):
    return _process_inspection.linux_proc_path(pid)


def _linux_process_fields(pid):
    return _process_inspection.linux_process_fields(pid, proc_path_fn=_linux_proc_path)


def _process_start_identity(pid):
    return _process_inspection.process_start_identity(
        pid,
        windows_identity_fn=_windows_process_identity,
        linux_fields_fn=_linux_process_fields,
    )


def _process_matches(pid, identity):
    return _process_inspection.process_matches(
        pid,
        identity,
        pid_alive_fn=_pid_alive,
        start_identity_fn=_process_start_identity,
    )


def _linux_group_has_non_zombie_member(group_id, proc_root=Path("/proc")):
    return _process_inspection.linux_group_has_non_zombie_member(group_id, proc_root)


def _posix_group_alive(group_id):
    return _process_inspection.posix_group_alive(
        group_id,
        linux_non_zombie_fn=_linux_group_has_non_zombie_member,
    )


def _wait_until(predicate, timeout, interval=0.05):
    return _process_inspection.wait_until(predicate, timeout, interval)


def _windows_process_table():
    return _process_inspection.windows_process_table()


def _windows_descendants(root_pid):
    return _process_inspection.windows_descendants(
        root_pid, process_table_fn=_windows_process_table
    )


def _windows_terminate_pid(pid, identity=""):
    return _process_inspection.windows_terminate_pid(
        pid,
        identity,
        process_matches_fn=_process_matches,
        pid_alive_fn=_pid_alive,
        windows_identity_fn=_windows_process_identity,
    )


def _write_private_file(path, value):
    _state_store.write_private_file(path, value)


def _provision_health_token(path, configured=""):
    return _state_store.provision_health_token(path, configured)


class LauncherController:
    """Host-controlled launcher with a persistent, single-operation queue."""

    def __init__(
        self,
        root=ROOT,
        python=sys.executable,
        server_host=DEFAULT_SERVER_HOST,
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
        if not _loopback(server_host):
            raise ValueError(
                "managed Sonder API host must be loopback; use a TLS reverse proxy "
                "for remote access"
            )
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
        return _state_store.connect_database(self.db_path)

    def _initialize_database(self):
        _state_store.initialize_database(self.db_path, self._connect)

    def _provision_controller_health_token(self, configured):
        return _state_store.provision_controller_health_token(
            self._connect,
            self.health_token_path,
            configured,
            provision_fn=_provision_health_token,
        )

    @staticmethod
    def _operation_from_row(row):
        return _state_store.operation_from_row(row)

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
        return _process_inspection.extract_control_metadata(operation)

    def _terminate_control_tree(self, operation, process=None):
        hooks = _process_inspection.ControlTreeHooks(
            pid_alive=_pid_alive,
            process_matches=_process_matches,
            process_start_identity=_process_start_identity,
            posix_group_alive=_posix_group_alive,
            windows_descendants=_windows_descendants,
            windows_terminate_pid=_windows_terminate_pid,
            terminate_grace_seconds=CONTROL_TERMINATE_GRACE_SECONDS,
            kill_grace_seconds=CONTROL_KILL_GRACE_SECONDS,
        )
        return _process_inspection.terminate_control_tree(
            operation, process=process, hooks=hooks
        )

    def _recovery_blocked(self, operation_id, owner_id):
        _state_store.mark_recovery_blocked(self._connect, operation_id, owner_id)

    def _interrupt_recovered_operation(self, lock, operation, reason):
        return _state_store.interrupt_recovered_operation(
            self._connect, lock, operation, reason
        )

    def recover_interrupted(self):
        """Reconcile stale control work before releasing its singleton lock."""
        lock, operation = _state_store.load_lock_and_operation(self._connect)
        if not lock.get("operation_id"):
            return
        if operation is None:
            # Missing ownership metadata is corruption, not proof that no child
            # exists. Fail closed and retain the lock for manual inspection.
            return
        if operation["phase"] not in ACTIVE_PHASES:
            if not self._terminate_control_tree(operation):
                return
            _state_store.clear_operation_lock(
                self._connect, operation["id"], lock.get("owner_id")
            )
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
        _state_store.persist_control_started(
            self._connect,
            operation_id,
            owner_id,
            pid,
            identity,
            group_id,
            platform_name,
            retry_delays=FINALIZE_RETRY_DELAYS,
        )

    def _persist_control_finished(self, pid, identity, return_code):
        context = self._current_operation_context()
        if context is None:
            return
        operation_id, owner_id = context
        _state_store.persist_control_finished(
            self._connect,
            operation_id,
            owner_id,
            pid,
            identity,
            return_code,
            retry_delays=FINALIZE_RETRY_DELAYS,
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
        """Return healthy, stopped, foreign_listener or indeterminate (see launcher_probe)."""
        if not _reachable(self.server_host, self.server_port):
            return "stopped"
        nonce = sonder_health.new_nonce()
        url_host = self.server_host
        if ":" in url_host and not url_host.startswith("["):
            url_host = "[%s]" % url_host
        request = urllib.request.Request(
            "http://%s:%s%s" % (url_host, self.server_port, sonder_health.PATH),
            headers={sonder_health.NONCE_HEADER: nonce},
            method="GET",
        )
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(request, timeout=0.8) as response:
                if response.status != 200:
                    return "foreign_listener"
                raw = response.read(4097)
        except (OSError, urllib.error.URLError, ValueError) as exc:
            return probe_error_state(exc)
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
        return _state_store.latest_operations(self._connect)

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
        return _state_store.load_operation(self._connect, operation_id)

    def operation_payload(self, operation):
        if not operation:
            return None
        return _state_store.build_operation_payload(self.status(), operation)

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
        operation, created = _state_store.submit_operation_record(
            self._connect,
            operation_id=operation_id,
            owner_id=owner_id,
            action=action,
            context_size=context_size,
            idempotency_key=idempotency_key,
            now=now,
            lease_until=lease_until,
            hard_deadline=hard_deadline,
            register_worker_fn=self._register_worker,
            unregister_worker_fn=self._unregister_worker,
        )
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
        return operation, created

    def _operation_worker(
        self, operation_id, owner_id, action, context_size, timeout
    ):
        try:
            if not _state_store.mark_operation_running(
                self._connect, operation_id, owner_id
            ):
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
        return _state_store.finish_operation_with_retry(
            operation_id=operation_id,
            owner_id=owner_id,
            phase=phase,
            message=message,
            last_error=last_error,
            commands=commands,
            retry_delays=FINALIZE_RETRY_DELAYS,
            finish_fn=self._finish_operation,
            emergency_finish_fn=self._emergency_finish_operation,
        )

    def _emergency_finish_operation(
        self, operation_id, owner_id, phase, message, last_error, commands
    ):
        return _state_store.emergency_finish_operation(
            self.db_path,
            operation_id,
            owner_id,
            phase,
            message,
            last_error,
            commands,
            max_operation_output=MAX_OPERATION_OUTPUT,
            output_text_fn=_output_text,
        )

    def _finish_operation(
        self, operation_id, owner_id, phase, message, last_error, commands
    ):
        return _state_store.finish_operation(
            self._connect,
            operation_id,
            owner_id,
            phase,
            message,
            last_error,
            commands,
            retention=self.retention,
            max_operation_output=MAX_OPERATION_OUTPUT,
            output_text_fn=_output_text,
        )

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
            initial_state = settle_probe(self._server_state, POLL_INTERVAL_SECONDS)
            if initial_state in ("foreign_listener", "indeterminate"):
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


_LAUNCHER_MACHINE_NAMES = None
_LAUNCHER_MACHINE_NAMES_LOCK = threading.Lock()


def launcher_machine_names(timeout=1.0):
    """This machine's own Host names, computed once (bounded FQDN lookup)."""
    global _LAUNCHER_MACHINE_NAMES
    with _LAUNCHER_MACHINE_NAMES_LOCK:
        if _LAUNCHER_MACHINE_NAMES is None:
            try:
                hostname = socket.gethostname()
            except OSError:
                hostname = ""
            fqdn = ""
            if hostname:
                value, error, finished = run_bounded(
                    lambda: socket.getfqdn(hostname), timeout,
                    name="sonder-launcher-fqdn",
                )
                if finished and error is None and isinstance(value, str):
                    fqdn = value
            _LAUNCHER_MACHINE_NAMES = machine_host_names(hostname, fqdn)
        return _LAUNCHER_MACHINE_NAMES


def parse_launcher_allowed_hosts(value):
    """``SONDER_LAUNCHER_ALLOWED_HOSTS``: comma-separated names; junk is dropped."""
    parsed = []
    for part in str(value or "").split(","):
        if not part.strip():
            continue
        try:
            parsed.append(normalize_allowed_host(part.strip()))
        except ValueError:
            print("ignoring malformed SONDER_LAUNCHER_ALLOWED_HOSTS entry", file=sys.stderr)
    return tuple(dict.fromkeys(parsed))


class LauncherServer(BoundedConnectionsMixin, deferred_tls.DeferredTLSHandshakeMixin, ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    max_connections = 64

    def __init__(
        self, address, handler, *, controller, token, command_journal=None,
        allowed_hosts=None, local_names=None,
    ):
        super().__init__(address, handler)
        self.controller = controller
        self.token = token
        # DNS-rebinding defence, same policy as the main listener: addresses
        # and this machine's names always; other names only when a token is
        # required or they are listed in SONDER_LAUNCHER_ALLOWED_HOSTS.
        self.allowed_hosts = (
            parse_launcher_allowed_hosts(os.environ.get("SONDER_LAUNCHER_ALLOWED_HOSTS", ""))
            if allowed_hosts is None else tuple(allowed_hosts)
        )
        self.local_names = launcher_machine_names() if local_names is None else frozenset(local_names)
        if command_journal is None and getattr(controller, "db_path", None):
            journal_path = os.environ.get(
                "SONDER_LAUNCHER_COMMAND_JOURNAL", ""
            ).strip() or str(
                Path(controller.db_path).with_name(
                    "sonder-launcher-command-journal.jsonl"
                )
            )
            command_journal = command_recovery.CommandJournal(journal_path)
        self.command_journal = command_journal


class LauncherHandler(LauncherHTTPHandler):
    host_not_allowed_remedy = HOST_NOT_ALLOWED_REMEDY
    command_conflict_type = command_recovery.CommandConflict
    command_journal_error_type = command_recovery.CommandJournalError
    conflict_error_type = LauncherConflictError

    def _max_body(self):
        return MAX_BODY

    def _body_read_timeout(self):
        return BODY_READ_TIMEOUT_SECONDS

    def _host_decision(
        self,
        host_header,
        *,
        allowed_hosts,
        local_names,
        credentials_required,
    ):
        return host_decision(
            host_header,
            allowed_hosts=allowed_hosts,
            local_names=local_names,
            credentials_required=credentials_required,
        )


def validate_configuration(
    host, token, cert="", key="", allow_insecure_http_for_development=False
):
    loopback = _loopback(host)
    if not loopback and len(token) < 24:
        raise ValueError(
            "LAN launcher binding requires SONDER_LAUNCHER_TOKEN with at least 24 characters"
        )
    if bool(cert) != bool(key):
        raise ValueError("both TLS certificate and key are required")
    if (
        not loopback
        and not cert
        and not allow_insecure_http_for_development
    ):
        raise ValueError(
            "non-loopback launcher binding requires TLS; use --cert and --key "
            "or the explicit development-only insecure HTTP override"
        )


def serve(
    host,
    port,
    token,
    controller=None,
    cert="",
    key="",
    allow_insecure_http_for_development=False,
):
    validate_configuration(host, token, cert, key, allow_insecure_http_for_development)
    context = None
    if cert:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(cert, key)
    server = LauncherServer(
        (host, int(port)), LauncherHandler,
        controller=controller or LauncherController(), token=token,
    )
    if context is not None:
        server.socket = deferred_tls.wrap_listener(context, server.socket)  # no accept() stall
    warning = unauthenticated_loopback_warning(host, token)
    if warning:
        print(warning, file=sys.stderr, flush=True)
    print("Sonder launcher listening on %s://%s:%s" % ("https" if cert else "http", host, port))
    server.serve_forever()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("SONDER_LAUNCHER_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SONDER_LAUNCHER_PORT", DEFAULT_PORT)))
    parser.add_argument("--token", default=os.environ.get("SONDER_LAUNCHER_TOKEN", ""))
    parser.add_argument(
        "--server-host", default=os.environ.get("SONDER_HOST", DEFAULT_SERVER_HOST)
    )
    parser.add_argument("--server-port", type=int, default=int(os.environ.get("SONDER_PORT", SERVER_PORT)))
    parser.add_argument("--cert", default=os.environ.get("SONDER_LAUNCHER_CERT", ""))
    parser.add_argument("--key", default=os.environ.get("SONDER_LAUNCHER_KEY", ""))
    parser.add_argument(
        "--allow-insecure-http-for-development",
        action="store_true",
        help="allow plaintext HTTP on a non-loopback development network",
    )
    parser.add_argument("--generate-token", action="store_true")
    args = parser.parse_args(argv)
    if args.generate_token:
        print(generate_token())
        return 0
    try:
        validate_configuration(
            args.host,
            args.token,
            args.cert,
            args.key,
            args.allow_insecure_http_for_development,
        )
        controller = LauncherController(
            server_host=args.server_host, server_port=args.server_port
        )
        serve(
            args.host,
            args.port,
            args.token,
            controller,
            args.cert,
            args.key,
            args.allow_insecure_http_for_development,
        )
    except (OSError, ValueError, sqlite3.Error) as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2
    return 0


__all__ = [
    "LauncherConflictError",
    "ControlTreeNotStopped",
    "LauncherController",
    "LauncherServer",
    "LauncherHandler",
    "generate_token",
    "validate_configuration",
    "unauthenticated_loopback_warning",
    "serve",
    "main",
    "launcher_machine_names",
    "parse_launcher_allowed_hosts",
    "signal",
]


if __name__ == "__main__":
    raise SystemExit(main())
