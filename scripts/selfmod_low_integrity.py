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
    finally:
        state["output_tail"] = b"".join(chunks)[-_OUTPUT_TAIL_BYTES:]
        _write_output_tail(output_path, chunks, total)
        stream.close()


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


def run_isolated(
    command: Sequence[str], *, cwd: str | os.PathLike[str], timeout: int,
    protected_paths: Sequence[str | os.PathLike[str]] = (),
) -> dict[str, object]:
    """Run ``command`` below low MIC and return a selfmod-compatible result."""
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
    work = Path(tempfile.mkdtemp(prefix="sonder-selfmod-low-"))
    try:
        # A low object is writable by the low candidate; the evaluator files
        # remain medium with NO_READ_UP|NO_WRITE_UP below.
        _label(work, "WinLowLabelSid", 0)
        output = work / "output.bin"
        result_path = work / "result.json"
        spec_path = work / "spec.json"
        low_home = work / "home"
        home_drive, home_tail = os.path.splitdrive(str(low_home))
        env = {
            "SystemRoot": os.environ.get("SystemRoot", r"C:\Windows"),
            "WINDIR": os.environ.get("WINDIR", r"C:\Windows"),
            "PATH": os.environ.get("PATH", ""),
            "TEMP": str(work), "TMP": str(work),
            "USERPROFILE": str(low_home), "HOME": str(low_home),
            "HOMEDRIVE": home_drive, "HOMEPATH": home_tail,
            "APPDATA": str(low_home / "AppData" / "Roaming"),
            "LOCALAPPDATA": str(low_home / "AppData" / "Local"),
            "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPYCACHEPREFIX": str(work / "pycache"),
        }
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
            basic["ActiveProcessLimit"] = 16
            basic["LimitFlags"] |= (
                win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                | win32job.JOB_OBJECT_LIMIT_ACTIVE_PROCESS
                | win32job.JOB_OBJECT_LIMIT_PROCESS_MEMORY
                | win32job.JOB_OBJECT_LIMIT_JOB_MEMORY
            )
            limits["ProcessMemoryLimit"] = 2 * 1024 ** 3
            limits["JobMemoryLimit"] = 4 * 1024 ** 3
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
        after = {str(path): _digest(path) for path in protected if path.is_file()}
        if before != after:
            return {"exit_code": 2, "output": output_text + "\nSELFMOD EVALUATOR CANARY FAILED: protected truth changed\n", "passed": False}
        if timed_out:
            code = 124
        return {"exit_code": int(code), "output": output_text, "passed": int(code) == 0}
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
