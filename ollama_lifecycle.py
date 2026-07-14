"""Narrow lifecycle helpers for releasing local Ollama resources.

Ollama 0.31.x can leave its short-lived Windows ``llama-server`` GPU
discovery probes behind when the discovery watchdog times out under memory
pressure.  These helpers deliberately distinguish those no-model probes from
real model runners and standalone llama.cpp servers.  Discovery probes may be
reclaimed after identity-stable verification; model runners additionally need
an explicit opt-in, an authoritative empty Ollama residency result, and a
trusted content-addressed model path.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

import process_liveness


_DISCOVERY_SERVER = re.compile(
    r"(?:^|\s)--port(?:\s+|=)\d+.*(?:^|\s)--host(?:\s+|=)127\.0\.0\.1(?:\s|$)",
    re.IGNORECASE,
)
_MODEL_ARGUMENT = re.compile(r"(?:^|\s)--model(?:\s+|=)", re.IGNORECASE)
_MODEL_VALUE = re.compile(
    r'(?:^|\s)--model(?:\s+|=)(?:"([^"]+)"|(\S+))', re.IGNORECASE,
)
_OLLAMA_BLOB = re.compile(r"^sha256-[0-9a-f]{64}$", re.IGNORECASE)


def _is_windows() -> bool:
    return os.name == "nt"


def resident_models(payload: object) -> set[str]:
    """Return case-folded model names from an Ollama ``/api/ps`` payload."""
    if not isinstance(payload, dict):
        return set()
    rows = payload.get("models")
    if not isinstance(rows, list):
        return set()
    names: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or row.get("model") or "").strip()
        if name:
            names.add(name.casefold())
    return names


def _trusted_ollama_roots() -> tuple[Path, ...]:
    candidates: list[Path] = []
    configured = os.environ.get("SONDER_OLLAMA_EXE", "").strip()
    if configured:
        candidates.append(Path(configured).expanduser().parent)
    found = shutil.which("ollama")
    if found:
        candidates.append(Path(found).parent)
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        candidates.append(Path(local_app_data) / "Programs" / "Ollama")

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            normalized = candidate.resolve(strict=False)
        except OSError:
            continue
        key = os.path.normcase(str(normalized))
        if key not in seen:
            seen.add(key)
            unique.append(normalized)
    return tuple(unique)


def _trusted_model_roots() -> tuple[Path, ...]:
    """Return only roots Ollama itself uses for content-addressed blobs."""
    candidates: list[Path] = []
    configured = os.environ.get("OLLAMA_MODELS", "").strip()
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.append(Path.home() / ".ollama" / "models")

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            normalized = candidate.resolve(strict=False)
        except OSError:
            continue
        key = os.path.normcase(str(normalized))
        if key not in seen:
            seen.add(key)
            unique.append(normalized)
    return tuple(unique)


def _path_is_within(path: str, roots: tuple[Path, ...]) -> bool:
    if not path or not roots:
        return False
    try:
        candidate = Path(path).resolve(strict=False)
    except OSError:
        return False
    candidate_text = os.path.normcase(str(candidate))
    for root in roots:
        root_text = os.path.normcase(str(root))
        try:
            if os.path.commonpath((candidate_text, root_text)) == root_text:
                return True
        except ValueError:
            continue
    return False


def _windows_llama_processes(runner=subprocess.run) -> list[dict]:
    """Read exact llama-server process metadata without mutating processes."""
    if not _is_windows():
        return []
    script = (
        "$all=@(Get-CimInstance Win32_Process);$ids=@{};"
        "foreach($p in $all){$ids[[int]$p.ProcessId]=$true};"
        "@($all|Where-Object{$_.Name -ieq 'llama-server.exe'}|"
        "ForEach-Object{[pscustomobject]@{"
        "ProcessId=[int]$_.ProcessId;ParentProcessId=[int]$_.ParentProcessId;"
        "ParentAlive=$ids.ContainsKey([int]$_.ParentProcessId);"
        "ExecutablePath=[string]$_.ExecutablePath;CommandLine=[string]$_.CommandLine"
        "}})|ConvertTo-Json -Compress"
    )
    try:
        result = runner(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    if result.returncode != 0 or not (result.stdout or "").strip():
        return []
    try:
        decoded = json.loads(result.stdout)
    except (TypeError, ValueError):
        return []
    rows = decoded if isinstance(decoded, list) else [decoded]
    return [row for row in rows if isinstance(row, dict)]


def _is_orphaned_discovery_probe(
    row: dict, trusted_roots: tuple[Path, ...],
) -> bool:
    try:
        pid = int(row.get("ProcessId") or 0)
    except (TypeError, ValueError, OverflowError):
        return False
    if pid <= 0 or bool(row.get("ParentAlive")):
        return False
    if not _is_trusted_llama_process(row, trusted_roots):
        return False
    command = str(row.get("CommandLine") or "").strip()
    if not command or _MODEL_ARGUMENT.search(command):
        return False
    lowered = command.casefold()
    list_probe = "--list-devices" in lowered and "--offline" in lowered
    server_probe = (
        bool(_DISCOVERY_SERVER.search(command))
        and "--no-webui" in lowered
        and "--offline" in lowered
        and "--verbose" in lowered
    )
    return list_probe or server_probe


def _is_trusted_llama_process(
    row: dict, trusted_roots: tuple[Path, ...],
) -> bool:
    executable = str(row.get("ExecutablePath") or "").strip()
    return (
        bool(executable)
        and Path(executable).name.casefold() == "llama-server.exe"
        and _path_is_within(executable, trusted_roots)
    )


def _is_orphaned_model_runner(
    row: dict, trusted_binary_roots: tuple[Path, ...],
    trusted_model_roots: tuple[Path, ...],
) -> bool:
    """Recognize an unmanaged Ollama runner for a content-addressed blob.

    This is intentionally narrower than merely seeing ``--model``.  The
    executable must be Ollama's bundled server, its parent must have vanished,
    and the model must be a sha256 blob below Ollama's configured model root.
    """
    if bool(row.get("ParentAlive")) or not _is_trusted_llama_process(
        row, trusted_binary_roots,
    ):
        return False
    command = str(row.get("CommandLine") or "").strip()
    match = _MODEL_VALUE.search(command)
    if not match:
        return False
    model_path = (match.group(1) or match.group(2) or "").strip()
    if not model_path or not _path_is_within(model_path, trusted_model_roots):
        return False
    if not _OLLAMA_BLOB.fullmatch(Path(model_path).name):
        return False
    lowered = command.casefold()
    return (
        bool(_DISCOVERY_SERVER.search(command))
        and "--no-webui" in lowered
        and "--offline" in lowered
    )


def _terminate_windows_process(pid: int, expected_identity: str) -> bool:
    """Terminate one already-verified Windows process without PID-reuse races."""
    if not _is_windows() or not expected_identity:
        return False
    state, identity = process_liveness.probe_process(pid)
    if state != process_liveness.PROCESS_ALIVE or identity != expected_identity:
        return False
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.TerminateProcess.argtypes = (ctypes.c_void_p, ctypes.c_uint)
        kernel32.TerminateProcess.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.OpenProcess(0x0001 | 0x1000, False, int(pid))
        if not handle:
            return False
        try:
            # Recheck the creation-time identity immediately before mutation.
            current_state, current_identity = process_liveness.probe_process(pid)
            if (
                current_state != process_liveness.PROCESS_ALIVE
                or current_identity != expected_identity
            ):
                return False
            return bool(kernel32.TerminateProcess(handle, 1))
        finally:
            kernel32.CloseHandle(handle)
    except (AttributeError, ImportError, OSError, TypeError, ValueError):
        return False


def cleanup_orphaned_discovery_probes(
    *,
    grace_seconds: float = 5.5,
    poll_seconds: float = 0.25,
    inspector=None,
    terminator=None,
    sleeper=time.sleep,
    allow_model_runners: bool = False,
) -> dict:
    """Remove only orphaned, no-model Ollama GPU discovery probes on Windows.

    The grace window covers Ollama's three-second refresh watchdog.  A candidate
    must appear in two consecutive inspections with the same process creation
    identity before termination.  Orphaned model runners are protected by
    default and are eligible only when ``allow_model_runners`` is explicitly
    true after the caller has independently confirmed no resident models.
    """
    result = {
        "terminated": [],
        "terminated_model_runners": [],
        "protected_model_runners": [],
        "errors": [],
    }
    if not _is_windows():
        return result
    inspect = inspector or _windows_llama_processes
    terminate = terminator or _terminate_windows_process
    roots = _trusted_ollama_roots()
    model_roots = _trusted_model_roots()
    if not roots:
        result["errors"].append("no trusted Ollama installation root")
        return result

    def classify(rows):
        candidates: dict[int, str] = {}
        model_candidates: dict[int, str] = {}
        protected: set[int] = set()
        for row in rows:
            try:
                pid = int(row.get("ProcessId") or 0)
            except (TypeError, ValueError, OverflowError):
                continue
            if pid <= 0 or bool(row.get("ParentAlive")):
                continue
            if not _is_trusted_llama_process(row, roots):
                continue
            command = str(row.get("CommandLine") or "")
            if _MODEL_ARGUMENT.search(command):
                if allow_model_runners and _is_orphaned_model_runner(
                    row, roots, model_roots,
                ):
                    state, identity = process_liveness.probe_process(pid)
                    if state == process_liveness.PROCESS_ALIVE and identity:
                        model_candidates[pid] = identity
                else:
                    protected.add(pid)
                continue
            if not _is_orphaned_discovery_probe(row, roots):
                continue
            state, identity = process_liveness.probe_process(pid)
            if state == process_liveness.PROCESS_ALIVE and identity:
                candidates[pid] = identity
        return candidates, model_candidates, protected

    # One delayed probe is cheaper and less disruptive than repeatedly starting
    # PowerShell/CIM while the machine is already under memory pressure.  A
    # second observation protects against both natural exit and PID reuse.
    grace = max(0.0, float(grace_seconds))
    if grace:
        sleeper(grace)
    first, first_models, protected = classify(inspect())
    result["protected_model_runners"] = sorted(protected)
    if first or first_models:
        sleeper(max(0.01, float(poll_seconds)))
        second, second_models, protected = classify(inspect())
        result["protected_model_runners"] = sorted(
            set(result["protected_model_runners"]) | protected
        )
        for pid, identity in first.items():
            if second.get(pid) != identity:
                continue
            if terminate(pid, identity):
                result["terminated"].append(pid)
            else:
                result["errors"].append(
                    "could not terminate verified discovery probe pid %d" % pid
                )
        for pid, identity in first_models.items():
            if second_models.get(pid) != identity:
                continue
            if terminate(pid, identity):
                result["terminated_model_runners"].append(pid)
            else:
                result["errors"].append(
                    "could not terminate verified orphan model runner pid %d" % pid
                )
    result["terminated"] = sorted(set(result["terminated"]))
    result["terminated_model_runners"] = sorted(
        set(result["terminated_model_runners"])
    )
    return result
