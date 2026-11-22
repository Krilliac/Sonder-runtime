"""Subprocess boundary for scenario validation adapters."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import sonder_runtime.adapters.process_tree as process_tree
from sonder_runtime.platform.runtime_threads import Thread as owned_runtime_thread


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
        kwargs.update(process_tree.group_popen_kwargs())
        process = None
        tree = None
        started_readers = []
        try:
            process = subprocess.Popen(list(command), **kwargs)
            tree = (
                process_tree.ProcessTree.owning(process)
                if process_tree.is_real_popen(process) else None
            )
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
                owned_runtime_thread(target=capture, args=(process.stdout, stdout_buffer), daemon=True),
                owned_runtime_thread(target=capture, args=(process.stderr, stderr_buffer), daemon=True),
            ]
            for reader in readers:
                reader.start()
                started_readers.append(reader)
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
                self._terminate(process, tree)
                for reader in started_readers:
                    reader.join(timeout=5)
                return ProcessResult(None, self._decode(stdout_buffer), self._decode(stderr_buffer), True, "TimeoutExpired")
            if overflow.is_set() or (tree is not None and tree.has_survivors()):
                # Also covers a command that exited but left descendants.
                self._terminate(process, tree)
            for reader in started_readers:
                reader.join(timeout=5)
            return ProcessResult(process.returncode, self._decode(stdout_buffer), self._decode(stderr_buffer), False, "OutputLimitExceeded" if overflow.is_set() else "")
        except (OSError, ValueError) as exc:
            if process is not None:
                self._terminate(process, tree)
            return ProcessResult(None, "", "", False, type(exc).__name__)
        finally:
            if process is not None and process.poll() is None:
                try:
                    self._terminate(process, tree)
                except OSError:
                    try:
                        process.kill()
                    except OSError:
                        pass
                if process.poll() is None:
                    try:
                        process.wait(timeout=5)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
            for reader in started_readers:
                reader.join(timeout=1)
            if tree is not None:
                tree.close()

    @staticmethod
    def _terminate(process, tree=None) -> None:
        """Remove the command's whole tree and reap it (see ``process_tree``)."""
        if tree is None:
            tree = process_tree.ProcessTree.adopt(process)
        tree.terminate(grace_seconds=0.2, kill_wait_seconds=5.0)

    @staticmethod
    def _decode(value: bytearray) -> str:
        return bytes(value).decode("utf-8", "replace").replace("\r\n", "\n")
