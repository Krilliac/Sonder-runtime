"""Subprocess boundary for playtest adapters."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProcessResult:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    error_type: str = ""


class ProcessAdapter:
    def run(self, command: tuple[str, ...], *, cwd: Path, timeout_seconds: float) -> ProcessResult:
        allowed = {
            name: value for name, value in os.environ.items()
            if name.upper() in {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL"}
        }
        kwargs = {
            "cwd": cwd, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
            "text": True, "shell": False, "env": allowed,
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        try:
            process = subprocess.Popen(list(command), **kwargs)
            stdout, stderr = process.communicate(timeout=timeout_seconds)
            return ProcessResult(process.returncode, stdout, stderr)
        except subprocess.TimeoutExpired as exc:
            cleanup_error = ""
            if os.name == "nt":
                try:
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, text=True, check=False, timeout=5)
                except (OSError, subprocess.TimeoutExpired) as cleanup:
                    cleanup_error = type(cleanup).__name__
            else:
                try:
                    os.killpg(process.pid, 9)
                except OSError:
                    try:
                        process.kill()
                    except OSError as cleanup:
                        cleanup_error = type(cleanup).__name__
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                cleanup_error = cleanup_error or "communicate-timeout"
                process.kill()
                stdout, stderr = process.communicate(timeout=5)
            return ProcessResult(None, str(stdout or exc.stdout or ""), str(stderr or exc.stderr or ""), True, "TimeoutExpired" + (f"; cleanup={cleanup_error}" if cleanup_error else ""))
        except (OSError, ValueError) as exc:
            process = locals().get("process")
            if process is not None:
                process.kill()
                process.communicate(timeout=5)
            return ProcessResult(None, "", "", False, type(exc).__name__)
