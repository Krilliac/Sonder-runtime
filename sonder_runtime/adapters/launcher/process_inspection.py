"""Cross-platform process inspection, liveness, and control-tree termination."""
from __future__ import annotations

import ctypes
import errno
import ipaddress
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict

from sonder_runtime.adapters.process_liveness import pid_alive as _process_pid_alive


DEFAULT_SERVER_PORT = 11435


class ControlTreeMetadata(TypedDict):
    """Normalized metadata for a launcher-managed control process tree."""

    pid: int
    identity: str
    group: int
    platform: str
    exit_code: int | None
    owner_host: str


@dataclass(frozen=True)
class ControlTreeHooks:
    """Injectable callbacks and grace timings for control-tree termination."""

    pid_alive: Callable[[object], bool]
    process_matches: Callable[[int, str], bool]
    process_start_identity: Callable[[int], str]
    posix_group_alive: Callable[[object], bool]
    windows_descendants: Callable[[int], set[int]]
    windows_terminate_pid: Callable[[int, str], bool]
    terminate_grace_seconds: float
    kill_grace_seconds: float


def is_loopback(host: object) -> bool:
    """Return whether ``host`` is a literal loopback address or ``localhost``."""
    value = str(host or "").strip().strip("[]").lower()
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def is_reachable(
    host: str = "127.0.0.1",
    port: int = DEFAULT_SERVER_PORT,
    timeout: float = 0.4,
) -> bool:
    """Return whether a TCP connection to ``(host, port)`` succeeds."""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def pid_alive(pid: object) -> bool:
    """Return whether ``pid`` currently refers to a live OS process."""
    return bool(_process_pid_alive(pid))


def _load_kernel32() -> Any:
    """Load Windows ``kernel32`` via ``ctypes.WinDLL`` without platform typing errors."""
    windll = getattr(ctypes, "WinDLL", None)
    if windll is None:
        raise AttributeError("ctypes.WinDLL is unavailable on this platform")
    return windll("kernel32", use_last_error=True)


def windows_process_identity(pid: int) -> str:
    """Return a stable Windows creation-time identity token for ``pid``."""
    try:
        class FileTime(ctypes.Structure):
            _fields_ = [
                ("low", ctypes.c_uint32),
                ("high", ctypes.c_uint32),
            ]

        kernel32 = _load_kernel32()
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


def linux_proc_path(pid: object) -> Path | None:
    """Resolve a namespace PID even when ``/proc`` is mounted from the host."""
    try:
        target_pid = int(str(pid)) if isinstance(pid, str) else int(pid)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return None
    try:
        namespace_inode = os.stat("/proc/self/ns/pid").st_ino
        entries = Path("/proc").iterdir()
    except OSError:
        return None
    direct = Path("/proc/%d" % target_pid)
    candidates = ([direct] if direct.exists() else []) + list(entries)
    seen: set[str] = set()
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
                if values and int(values[-1]) == target_pid:
                    return entry
                break
    return None


def linux_process_fields(
    pid: object,
    *,
    proc_path_fn: Callable[[object], Path | None] | None = None,
) -> dict[str, int | str] | None:
    """Read Linux procfs state, parent, group, session, and start ticks for ``pid``."""
    resolver = proc_path_fn if proc_path_fn is not None else linux_proc_path
    try:
        process_path = resolver(pid)
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
        result: dict[str, int | str] = {
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


def process_start_identity(
    pid: int,
    *,
    windows_identity_fn: Callable[[int], str] | None = None,
    linux_fields_fn: Callable[[object], dict[str, int | str] | None] | None = None,
) -> str:
    """Compute a start-time identity string that guards against PID reuse."""
    if os.name == "nt":
        win_fn = windows_identity_fn if windows_identity_fn is not None else windows_process_identity
        return win_fn(pid)
    fields_fn = linux_fields_fn if linux_fields_fn is not None else linux_process_fields
    fields = fields_fn(pid)
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


def process_matches(
    pid: int,
    identity: str,
    *,
    pid_alive_fn: Callable[[object], bool] | None = None,
    start_identity_fn: Callable[[int], str] | None = None,
) -> bool:
    """Return whether ``pid`` is alive and still matches ``identity``."""
    alive_fn = pid_alive_fn if pid_alive_fn is not None else pid_alive
    ident_fn = start_identity_fn if start_identity_fn is not None else process_start_identity
    return bool(identity) and alive_fn(pid) and ident_fn(pid) == identity


def linux_group_has_non_zombie_member(
    group_id: int,
    proc_root: Path = Path("/proc"),
) -> bool | None:
    """Return whether a Linux process group has a non-zombie member.

    ``killpg(gid, 0)`` also succeeds when the only remaining members are
    unreaped zombies.  That is useful existence information, but it must not
    keep a control-tree operation alive forever under a PID 1 that does not
    reap promptly.  ``None`` means procfs was unavailable or unreadable and
    callers should retain the portable killpg fallback.
    """
    if not sys.platform.startswith("linux"):
        return None
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return None
    inspected = False
    ambiguous = False
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "stat").read_text(encoding="ascii")
            end = raw.rfind(")")
            fields = raw[end + 2:].split() if end >= 0 else []
            if len(fields) < 3:
                ambiguous = True
                continue
            state = fields[0]
            process_group = int(fields[2])
        except OSError as exc:
            if isinstance(exc, FileNotFoundError) or exc.errno in (
                errno.ENOENT,
                errno.ESRCH,
            ):
                continue
            ambiguous = True
            continue
        except ValueError:
            ambiguous = True
            continue
        inspected = True
        if process_group == group_id and state not in ("Z", "X", "x"):
            return True
    if ambiguous or not inspected:
        return None
    return False


def posix_group_alive(
    group_id: object,
    *,
    linux_non_zombie_fn: Callable[[int], bool | None] | None = None,
) -> bool:
    """Return whether POSIX process group ``group_id`` has any live non-zombie member."""
    try:
        gid = int(str(group_id)) if isinstance(group_id, str) else int(group_id)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return False
    if gid <= 1:
        return False
    killpg_fn: Callable[[int, int], None] | None = getattr(os, "killpg", None)
    if killpg_fn is None:
        return False
    try:
        killpg_fn(gid, 0)
    except OSError as exc:
        return exc.errno == errno.EPERM
    probe_fn = (
        linux_non_zombie_fn
        if linux_non_zombie_fn is not None
        else linux_group_has_non_zombie_member
    )
    linux_alive = probe_fn(gid)
    return True if linux_alive is None else linux_alive


def wait_until(
    predicate: Callable[[], bool],
    timeout: float,
    interval: float = 0.05,
) -> bool:
    """Poll ``predicate`` until it returns truthy or ``timeout`` expires."""
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        if predicate():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(interval, remaining))


def windows_process_table() -> dict[int, int]:
    """Snapshot the Windows PID -> parent PID mapping using Toolhelp32."""
    if os.name != "nt":
        return {}
    try:
        class ProcessEntry(ctypes.Structure):
            _fields_ = [
                ("dwSize", ctypes.c_uint32),
                ("cntUsage", ctypes.c_uint32),
                ("th32ProcessID", ctypes.c_uint32),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", ctypes.c_uint32),
                ("cntThreads", ctypes.c_uint32),
                ("th32ParentProcessID", ctypes.c_uint32),
                ("pcPriClassBase", ctypes.c_int32),
                ("dwFlags", ctypes.c_uint32),
                ("szExeFile", ctypes.c_wchar * 260),
            ]

        kernel32 = _load_kernel32()
        kernel32.CreateToolhelp32Snapshot.argtypes = (
            ctypes.c_uint32,
            ctypes.c_uint32,
        )
        kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        kernel32.Process32FirstW.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(ProcessEntry),
        )
        kernel32.Process32FirstW.restype = ctypes.c_int32
        kernel32.Process32NextW.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(ProcessEntry),
        )
        kernel32.Process32NextW.restype = ctypes.c_int32
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.restype = ctypes.c_int32
        snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
        if snapshot == ctypes.c_void_p(-1).value:
            return {}
        try:
            entry = ProcessEntry()
            entry.dwSize = ctypes.sizeof(ProcessEntry)
            processes: dict[int, int] = {}
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


def windows_descendants(
    root_pid: int,
    *,
    process_table_fn: Callable[[], dict[int, int]] | None = None,
) -> set[int]:
    """Return all descendant PIDs of ``root_pid`` from the Windows process table."""
    table_fn = process_table_fn if process_table_fn is not None else windows_process_table
    table = table_fn()
    descendants: set[int] = set()
    frontier: set[int] = {int(root_pid)}
    while frontier:
        children = {
            pid
            for pid, parent in table.items()
            if parent in frontier and pid not in descendants
        }
        descendants.update(children)
        frontier = children
    return descendants


def windows_terminate_pid(
    pid: int,
    identity: str = "",
    *,
    process_matches_fn: Callable[[int, str], bool] | None = None,
    pid_alive_fn: Callable[[object], bool] | None = None,
    windows_identity_fn: Callable[[int], str] | None = None,
) -> bool:
    """Terminate Windows process ``pid`` after re-checking its creation identity."""
    matches_fn = process_matches_fn if process_matches_fn is not None else process_matches
    alive_fn = pid_alive_fn if pid_alive_fn is not None else pid_alive
    win_id_fn = (
        windows_identity_fn
        if windows_identity_fn is not None
        else windows_process_identity
    )
    if identity and not matches_fn(pid, identity):
        return False
    try:
        kernel32 = _load_kernel32()
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
            return not alive_fn(pid)
        try:
            if identity and win_id_fn(pid) != identity:
                return False
            return bool(kernel32.TerminateProcess(handle, 1))
        finally:
            kernel32.CloseHandle(handle)
    except (AttributeError, ImportError, OSError, ValueError):
        return False


def extract_control_metadata(operation: Mapping[str, Any]) -> ControlTreeMetadata:
    """Extract normalized control process metadata from an operation mapping."""
    raw_exit = operation.get("control_exit_code")
    exit_code = int(raw_exit) if raw_exit is not None else None
    return {
        "pid": int(operation.get("control_pid") or 0),
        "identity": str(operation.get("control_identity") or ""),
        "group": int(operation.get("control_group_id") or 0),
        "platform": str(operation.get("control_platform") or ""),
        "exit_code": exit_code,
        "owner_host": str(operation.get("owner_host") or ""),
    }


def terminate_control_tree(
    operation: Mapping[str, Any],
    process: subprocess.Popen[bytes] | None = None,
    *,
    hooks: ControlTreeHooks,
) -> bool:
    """Stop a launcher-owned control process tree safely without signalling reused PIDs."""
    metadata = extract_control_metadata(operation)
    pid = metadata["pid"]
    identity = metadata["identity"]
    group_id = metadata["group"]
    if not pid:
        return True

    root_matches = bool(
        process is not None
        and process.pid == pid
        and process.returncode is None
    ) or hooks.process_matches(pid, identity)
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
    if hooks.pid_alive(pid) and not root_matches:
        # PID reuse: never signal a process whose start identity differs.
        return False

    if os.name != "nt":
        getpgrp_fn: Callable[[], int] | None = getattr(os, "getpgrp", None)
        current_pgrp = getpgrp_fn() if getpgrp_fn is not None else -1
        if group_id != pid or group_id <= 1 or group_id == current_pgrp:
            return not root_matches and not hooks.posix_group_alive(group_id)
        if not root_matches and not hooks.posix_group_alive(group_id):
            return True
        killpg_fn: Callable[[int, int], None] | None = getattr(os, "killpg", None)
        if killpg_fn is None:
            return False
        try:
            killpg_fn(group_id, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError:
            return False
        if process is not None:
            try:
                process.wait(timeout=hooks.terminate_grace_seconds)
            except subprocess.TimeoutExpired:
                pass
        if not wait_until(
            lambda: not hooks.posix_group_alive(group_id),
            hooks.terminate_grace_seconds,
        ):
            sigkill = int(getattr(signal, "SIGKILL", 9))
            try:
                killpg_fn(group_id, sigkill)
            except ProcessLookupError:
                pass
            except OSError:
                return False
        if process is not None:
            try:
                process.wait(timeout=hooks.kill_grace_seconds)
            except subprocess.TimeoutExpired:
                return False
        return wait_until(
            lambda: not hooks.posix_group_alive(group_id),
            hooks.kill_grace_seconds,
        )

    descendants = hooks.windows_descendants(pid)
    if process is not None and root_matches:
        ctrl_break = getattr(signal, "CTRL_BREAK_EVENT", None)
        if ctrl_break is not None:
            try:
                process.send_signal(int(ctrl_break))
                process.wait(timeout=hooks.terminate_grace_seconds)
            except (AttributeError, OSError, subprocess.TimeoutExpired):
                pass
    descendants.update(hooks.windows_descendants(pid))
    for child_pid in sorted(descendants, reverse=True):
        child_identity = hooks.process_start_identity(child_pid)
        if child_identity:
            hooks.windows_terminate_pid(child_pid, child_identity)
    if root_matches:
        hooks.windows_terminate_pid(pid, identity)
    if process is not None:
        try:
            process.wait(timeout=hooks.kill_grace_seconds)
        except subprocess.TimeoutExpired:
            return False
    return wait_until(
        lambda: not hooks.process_matches(pid, identity)
        and not any(
            hooks.pid_alive(child)
            for child in descendants.union(hooks.windows_descendants(pid))
        ),
        hooks.kill_grace_seconds,
    )
