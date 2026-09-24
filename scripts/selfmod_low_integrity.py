"""Windows low-integrity supervisor for unattended self-mod checks.

The parent stays at the normal user integrity level and owns the evaluator
truth.  Candidate code runs with a restricted low-integrity token in a Job
Object whose lifetime is tied to this supervisor.  This is intentionally a
small, dependency-light boundary: unsupported platforms fail closed when the
caller requests isolation.
"""

from __future__ import annotations

import argparse
from collections import deque
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from typing import Sequence


_OUTPUT_TAIL_BYTES = 120_000


def _write_output_tail(path: Path, chunks: deque[bytes], total: int) -> None:
    """Publish a bounded output tail while the low child is still running."""
    data = b"".join(chunks)
    if len(data) > _OUTPUT_TAIL_BYTES:
        data = data[-_OUTPUT_TAIL_BYTES:]
    path.write_bytes(data)


def _drain_output(stream, output_path: Path, state: dict[str, object]) -> None:
    """Drain a pipe continuously so a noisy candidate cannot block on a full pipe."""
    chunks: deque[bytes] = deque()
    total = 0
    try:
        while True:
            # ``read`` on a buffered Windows pipe can wait for the requested
            # size, hiding a small early failure until the process exits.
            # ``read1`` returns currently available bytes while still
            # draining the pipe continuously.
            reader = getattr(stream, "read1", stream.read)
            chunk = reader(16 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            while total > _OUTPUT_TAIL_BYTES and chunks:
                removed = chunks.popleft()
                total -= len(removed)
                if total < _OUTPUT_TAIL_BYTES and removed:
                    keep = _OUTPUT_TAIL_BYTES - total
                    chunks.appendleft(removed[-keep:])
                    total += min(keep, len(removed))
            _write_output_tail(output_path, chunks, total)
    except Exception as exc:
        state["drain_error"] = type(exc).__name__
    finally:
        state["output_tail"] = b"".join(chunks)[-_OUTPUT_TAIL_BYTES:]
        try:
            _write_output_tail(output_path, chunks, total)
        except Exception as exc:
            state["drain_error"] = type(exc).__name__
        stream.close()


def _timeout_diagnostic(tail: bytes) -> str:
    """Return content-free phase metadata for a timed-out child.

    The bounded tail remains available for ordinary diagnostics. This marker
    gives the medium-integrity supervisor useful information when a quiet
    command reaches its deadline without persisting test names or prompts.
    """
    text = tail.decode("utf-8", "replace")
    if not text.strip():
        phase = "unknown"
    else:
        phase = "child-output"
        for line in reversed(text.splitlines()):
            lowered = line.casefold()
            if "collecting" in lowered or "test session starts" in lowered:
                phase = "collection-or-startup"
                break
            if any(token in lowered for token in (" passed", " failed", " skipped", " error")):
                phase = "test-progress"
                break
    return (
        "\nSELFMOD LOW TIMEOUT DIAGNOSTIC: phase=%s; output_tail_bytes=%d\n"
        % (phase, len(tail))
    )


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _label(path: Path, sid_name: str, mask: int) -> None:
    import win32security

    sid = win32security.CreateWellKnownSid(
        getattr(win32security, sid_name), None
    )
    sacl = win32security.ACL()
    sacl.AddMandatoryAce(win32security.ACL_REVISION, 0, mask, sid)
    win32security.SetNamedSecurityInfo(
        str(path), win32security.SE_FILE_OBJECT,
        win32security.LABEL_SECURITY_INFORMATION,
        None, None, None, sacl,
    )


def _low_token():
    import win32api
    import win32con
    import win32security

    current = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(), win32con.TOKEN_ALL_ACCESS
    )
    restricted = win32security.CreateRestrictedToken(
        current, win32security.DISABLE_MAX_PRIVILEGE, [], [], []
    )
    low_sid = win32security.CreateWellKnownSid(
        win32security.WinLowLabelSid, None
    )
    win32security.SetTokenInformation(
        restricted, win32security.TokenIntegrityLevel, (low_sid, 96)
    )
    return restricted


def _child(spec_path: Path) -> int:
    """Run the actual check. This process and all descendants are low."""
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    command = [str(item) for item in spec["command"]]
    output_path = Path(spec["output"]).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    env = {str(k): str(v) for k, v in spec["env"].items()}
    # Create these inside the low token so their integrity label permits the
    # candidate's ordinary per-user caches without exposing the real profile.
    for name in ("USERPROFILE", "APPDATA", "LOCALAPPDATA"):
        Path(env[name]).mkdir(parents=True, exist_ok=True)
    process = None
    reader = None
    try:
        process = subprocess.Popen(
            command, cwd=spec["cwd"], env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        assert process.stdout is not None
        state: dict[str, object] = {}
        reader = threading.Thread(
            target=_drain_output, args=(process.stdout, output_path, state),
            name="selfmod-output-drain", daemon=True,
        )
        reader.start()
        try:
            returncode = process.wait(timeout=max(1, int(spec["timeout"])))
            result = {"returncode": returncode, "timed_out": False}
        except subprocess.TimeoutExpired:
            process.kill()
            returncode = process.wait(timeout=10)
            result = {"returncode": 124, "timed_out": True}
        reader.join(timeout=10)
        if reader.is_alive():
            result = {"returncode": 125, "timed_out": False,
                      "error": "output drain did not terminate"}
        elif state.get("drain_error"):
            result = {"returncode": 125, "timed_out": False,
                      "error": "output drain failed: " + str(state["drain_error"])}
        if result.get("timed_out"):
            tail = bytes(state.get("output_tail") or b"")
            marker = _timeout_diagnostic(tail).encode("utf-8")
            output_path.write_bytes((tail + marker)[-_OUTPUT_TAIL_BYTES:])
            result["diagnostic"] = _timeout_diagnostic(tail).strip()
    except BaseException as exc:  # preserve a bounded diagnostic for parent
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        if reader is not None:
            reader.join(timeout=10)
        output_path.write_text("%s: %s\n" % (type(exc).__name__, exc), encoding="utf-8")
        result = {"returncode": 125, "timed_out": False, "error": str(exc)}
    Path(spec["result"]).write_text(json.dumps(result), encoding="utf-8")
    return int(result["returncode"])


# Non-secret host facts that ordinary Windows tooling needs to resolve
# executables and system locations. Without PATHEXT, for example, PowerShell's
# ``Get-Command python`` cannot find ``python.exe`` on PATH. Credentials,
# tokens, and the user's real profile/AppData locations are never copied.
_PASSTHROUGH_ENV = (
    "PATHEXT", "COMSPEC", "SystemDrive", "ProgramFiles", "ProgramFiles(x86)",
    "ProgramW6432", "ProgramData", "CommonProgramFiles",
    "CommonProgramFiles(x86)", "CommonProgramW6432", "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER", "OS",
)

# Read-only toolchain homes. Redirecting USERPROFILE makes rustup/cargo look in
# the disposable home and report "no default toolchain". The low token still
# cannot write these medium-integrity directories.
_TOOLCHAIN_HOMES = (("RUSTUP_HOME", ".rustup"), ("CARGO_HOME", ".cargo"))


def _low_environment(work: Path, low_home: Path) -> dict[str, str]:
    """Build the allowlisted environment for the low-integrity child."""
    home_drive, home_tail = os.path.splitdrive(str(low_home))
    env = {
        "SystemRoot": os.environ.get("SystemRoot", r"C:\Windows"),
        "WINDIR": os.environ.get("WINDIR", r"C:\Windows"),
        "PATH": os.environ.get("PATH", ""),
    }
    for name in _PASSTHROUGH_ENV:
        value = os.environ.get(name)
        if value:
            env[name] = value
    real_home = Path(os.environ.get("USERPROFILE") or Path.home())
    for name, default in _TOOLCHAIN_HOMES:
        value = os.environ.get(name) or str(real_home / default)
        if Path(value).is_dir():
            env[name] = value
    env.update({
        "TEMP": str(work), "TMP": str(work),
        "USERPROFILE": str(low_home), "HOME": str(low_home),
        "HOMEDRIVE": home_drive, "HOMEPATH": home_tail,
        "APPDATA": str(low_home / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(low_home / "AppData" / "Local"),
        "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPYCACHEPREFIX": str(work / "pycache"),
    })
    return env


_MIB = 1024 ** 2
DEFAULT_PROCESS_MEMORY_MB = 2048
DEFAULT_JOB_MEMORY_MB = 4096
DEFAULT_ACTIVE_PROCESSES = 32
# Hard ceilings keep a misconfigured caller from turning the Job into an
# unbounded one; they are well below the host's commit limit.
_MAX_PROCESS_MEMORY_MB = 16384
_MAX_JOB_MEMORY_MB = 24576
_MAX_ACTIVE_PROCESSES = 128


def _bounded(value, default: int, ceiling: int, name: str) -> int:
    number = default if value is None else int(value)
    if number < 1 or number > ceiling:
        raise ValueError("%s must be between 1 and %d" % (name, ceiling))
    return number


# Tests nest deep directories below TEMP (pytest-of-<user>/pytest-N/popen-gwN/
# <test name>/...). A long work root pushed working directories past
# MAX_PATH, and CreateProcess then failed with ERROR_DIRECTORY (267). Keep the
# root short and refuse to run from a root that is not.
MAX_WORK_ROOT_CHARS = 48
# CreateProcess requires the current directory to be shorter than MAX_PATH
# (260) including a trailing separator and terminator.
MAX_CHILD_CWD_CHARS = 258


def _short_work_dir() -> Path:
    """Create the per-run work directory under a short, user-owned root."""
    override = os.environ.get("SONDER_SELFMOD_SCRATCH_ROOT", "").strip()
    candidates = [Path(override)] if override else []
    candidates.append(Path(os.environ.get("USERPROFILE") or Path.home()) / ".sl")
    candidates.append(Path(tempfile.gettempdir()))
    for root in candidates:
        if len(str(root)) > MAX_WORK_ROOT_CHARS - 10:
            continue
        try:
            root.mkdir(parents=True, exist_ok=True)
            work = Path(tempfile.mkdtemp(prefix="w", dir=str(root)))
        except OSError:
            continue
        if len(str(work)) <= MAX_WORK_ROOT_CHARS:
            return work
        shutil.rmtree(work, ignore_errors=True)
    raise RuntimeError(
        "no scratch root shorter than %d characters is available; set "
        "SONDER_SELFMOD_SCRATCH_ROOT" % MAX_WORK_ROOT_CHARS
    )


def run_isolated(
    command: Sequence[str], *, cwd: str | os.PathLike[str], timeout: int,
    protected_paths: Sequence[str | os.PathLike[str]] = (),
    process_memory_mb: int | None = None, job_memory_mb: int | None = None,
    active_processes: int | None = None,
) -> dict[str, object]:
    """Run ``command`` below low MIC and return a selfmod-compatible result.

    The Job limits default to the historical 2 GiB/process, 4 GiB/job and 32
    processes. Callers that knowingly run heavier workloads (parallel pytest
    workers, an ML-stack import) pass explicit, bounded values; the result
    reports the configured limits and the observed peaks so a limit hit is
    diagnosable instead of looking like a candidate regression.

    There is deliberately no medium-integrity mode: a candidate process at
    medium integrity could rewrite its own tested bytes, the selfmod database
    or .git, and a Job object does not contain what it can start through
    WMI or the Task Scheduler.
    """
    cwd_text = str(Path(cwd).resolve())
    if len(cwd_text) > MAX_CHILD_CWD_CHARS:
        # CreateProcessAsUser rejects a current directory at or beyond
        # MAX_PATH with ERROR_DIRECTORY (267), which otherwise surfaces as an
        # opaque isolation failure.
        raise ValueError(
            "child working directory is %d characters; Windows requires at most "
            "%d (ERROR_DIRECTORY 267): %s" % (len(cwd_text), MAX_CHILD_CWD_CHARS, cwd_text[:80] + "...")
        )
    process_memory_mb = _bounded(process_memory_mb, DEFAULT_PROCESS_MEMORY_MB, _MAX_PROCESS_MEMORY_MB, "process_memory_mb")
    job_memory_mb = _bounded(job_memory_mb, DEFAULT_JOB_MEMORY_MB, _MAX_JOB_MEMORY_MB, "job_memory_mb")
    active_processes = _bounded(active_processes, DEFAULT_ACTIVE_PROCESSES, _MAX_ACTIVE_PROCESSES, "active_processes")
    if os.name != "nt":
        raise RuntimeError("low-integrity selfmod isolation requires Windows")
    try:
        import win32api
        import win32con
        import win32job
        import win32process
        import win32security
    except ImportError as exc:
        raise RuntimeError("pywin32 is required for low-integrity selfmod isolation") from exc

    protected = [Path(item).resolve() for item in protected_paths]
    before = {str(path): _digest(path) for path in protected if path.is_file()}
    work = _short_work_dir()
    try:
        # A low object is writable by the low candidate; the evaluator files
        # remain medium with NO_READ_UP|NO_WRITE_UP below.
        _label(work, "WinLowLabelSid", 0)
        output = work / "output.bin"
        result_path = work / "result.json"
        spec_path = work / "spec.json"
        low_home = work / "home"
        env = _low_environment(work, low_home)
        spec_path.write_text(json.dumps({
            "command": list(command), "cwd": str(Path(cwd).resolve()),
            "timeout": max(1, int(timeout)), "env": env,
            "output": str(output), "result": str(result_path),
        }), encoding="utf-8")
        _label(spec_path, "WinLowLabelSid", 0)
        _label(output, "WinLowLabelSid", 0) if output.exists() else None
        # Keep the digest manifest itself outside the candidate's editable
        # surface. The suite files stay readable to the low evaluator, while
        # the parent verifies their bytes against this medium-protected key.
        manifest = work / "truth-manifest.json"
        manifest.write_text(json.dumps(before, sort_keys=True), encoding="utf-8")
        _label(manifest, "WinMediumLabelSid", win32security.SYSTEM_MANDATORY_LABEL_NO_READ_UP | win32security.SYSTEM_MANDATORY_LABEL_NO_WRITE_UP)
        token = _low_token()
        job = process_handle = thread_handle = None
        try:
            # pywin32 requires a name; a fresh unshared random name prevents
            # candidate code from reopening a predictable job. Limit total
            # memory and descendants,
            # in addition to the wall-clock deadline below.
            job = win32job.CreateJobObject(None, "SonderSelfmod-" + uuid.uuid4().hex)
            limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
            basic = limits["BasicLimitInformation"]
            # Four pytest workers and their short-lived test subprocesses can
            # overlap. Keep a finite ceiling without misclassifying routine
            # test setup as a candidate regression.
            basic["ActiveProcessLimit"] = active_processes
            basic["LimitFlags"] |= (
                win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                | win32job.JOB_OBJECT_LIMIT_ACTIVE_PROCESS
                | win32job.JOB_OBJECT_LIMIT_PROCESS_MEMORY
                | win32job.JOB_OBJECT_LIMIT_JOB_MEMORY
            )
            limits["ProcessMemoryLimit"] = process_memory_mb * _MIB
            limits["JobMemoryLimit"] = job_memory_mb * _MIB
            win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)
            startup = win32process.STARTUPINFO()
            flags = win32con.CREATE_NO_WINDOW | win32con.CREATE_UNICODE_ENVIRONMENT | win32con.CREATE_SUSPENDED
            # The candidate checkout is the current directory.  Execute this
            # trusted file by absolute path with isolated Python startup so a
            # candidate module cannot shadow the supervisor via ``python -m``.
            command_line = subprocess.list2cmdline([
                sys.executable, "-I", str(Path(__file__).resolve()), "--child", str(spec_path),
            ])
            process_handle, thread_handle, _pid, _tid = win32process.CreateProcessAsUser(
                token, None, command_line, None, None, False, flags, env,
                str(Path(cwd).resolve()), startup,
            )
            try:
                win32job.AssignProcessToJobObject(job, process_handle)
            except Exception:
                win32process.TerminateProcess(process_handle, 125)
                raise
            win32process.ResumeThread(thread_handle)
            deadline = time.monotonic() + max(1, int(timeout)) + 10
            while win32process.GetExitCodeProcess(process_handle) == win32con.STILL_ACTIVE and time.monotonic() < deadline:
                time.sleep(0.05)
            timed_out = win32process.GetExitCodeProcess(process_handle) == win32con.STILL_ACTIVE
            if timed_out:
                win32process.TerminateProcess(process_handle, 124)
            code = win32process.GetExitCodeProcess(process_handle)
            try:
                used = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
                peaks = {
                    "peak_process_memory_mb": int(used.get("PeakProcessMemoryUsed", 0)) // _MIB,
                    "peak_job_memory_mb": int(used.get("PeakJobMemoryUsed", 0)) // _MIB,
                }
            except Exception:
                peaks = {}
        finally:
            for handle in (thread_handle, process_handle, job, token):
                if handle is not None:
                    win32api.CloseHandle(handle)
        # The low child controls these files.  Its result JSON is diagnostic
        # only; the trusted process handle supplies the exit status.  Read a
        # bounded tail without following a candidate-created reparse point or
        # loading an arbitrarily large file into the medium supervisor.
        output_text = ""
        if output.exists() and not output.is_symlink() and output.is_file():
            with output.open("rb") as stream:
                stream.seek(max(0, output.stat().st_size - 120000))
                output_text = stream.read(120000).decode("utf-8", "replace")
        job_report = {
            "integrity": "low",
            "limits": {"process_memory_mb": process_memory_mb, "job_memory_mb": job_memory_mb,
                       "active_processes": active_processes},
            **peaks,
        }
        after = {str(path): _digest(path) for path in protected if path.is_file()}
        if before != after:
            return {"exit_code": 2, "output": output_text + "\nSELFMOD EVALUATOR CANARY FAILED: protected truth changed\n", "passed": False, "job": job_report}
        if timed_out:
            code = 124
        return {"exit_code": int(code), "output": output_text, "passed": int(code) == 0, "job": job_report}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", type=Path)
    args = parser.parse_args(argv)
    if args.child:
        return _child(args.child)
    parser.error("--child is required")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
