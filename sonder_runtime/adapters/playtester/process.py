"""Subprocess boundary for playtest adapters."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
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
    max_output_bytes = 1_000_000

    def run(self, command: tuple[str, ...], *, cwd: Path, timeout_seconds: float) -> ProcessResult:
        allowed = {
            name: value for name, value in os.environ.items()
            if name.upper() in {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL"}
        }
        kwargs = {
            "cwd": cwd, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
            "text": False, "shell": False, "env": allowed,
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        try:
            process = subprocess.Popen(list(command), **kwargs)
            stdout_buffer = bytearray()
            stderr_buffer = bytearray()
            overflow = threading.Event()

            def capture(stream, buffer):
                while True:
                    chunk = stream.read(65536)
                    if not chunk:
                        return
                    remaining = self.max_output_bytes - len(buffer)
                    if remaining > 0:
                        buffer.extend(chunk[:remaining])
                    if len(chunk) > max(0, remaining):
                        overflow.set()

            readers = [
                threading.Thread(target=capture, args=(process.stdout, stdout_buffer), daemon=True),
                threading.Thread(target=capture, args=(process.stderr, stderr_buffer), daemon=True),
            ]
            for reader in readers:
                reader.start()
            deadline = time.monotonic() + timeout_seconds
            timed_out = False
            while process.poll() is None:
                if overflow.is_set():
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                time.sleep(0.01)
            if timed_out:
                self._terminate(process)
                for reader in readers:
                    reader.join(timeout=5)
                return ProcessResult(None, self._decode(stdout_buffer), self._decode(stderr_buffer), True, "TimeoutExpired")
            if overflow.is_set():
                self._terminate(process)
            for reader in readers:
                reader.join(timeout=5)
            return ProcessResult(process.returncode, self._decode(stdout_buffer), self._decode(stderr_buffer), False, "OutputLimitExceeded" if overflow.is_set() else "")
        except (OSError, ValueError) as exc:
            process = locals().get("process")
            if process is not None:
                self._terminate(process)
            return ProcessResult(None, "", "", False, type(exc).__name__)

    @staticmethod
    def _terminate(process) -> None:
        if os.name == "nt":
            try:
                stopped = subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True, text=True, check=False, timeout=5,
                )
                if stopped.returncode == 0:
                    process.wait(timeout=5)
                    return
            except (OSError, subprocess.TimeoutExpired):
                pass
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
                return
            except (OSError, subprocess.TimeoutExpired):
                pass
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    @staticmethod
    def _decode(value: bytearray) -> str:
        return bytes(value).decode("utf-8", "replace").replace("\r\n", "\n")
