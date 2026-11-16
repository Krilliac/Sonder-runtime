"""Measure the host memory a loaded Ollama model really holds.

The runner process (``llama-server`` or ``ollama runner``) keeps weights,
a KV-cache mirror and compute buffers in host memory even when every layer sits
in VRAM; one Q8 9B model was seen holding about 17 GB. The only way to know a
model's footprint is to read its runner process, and finding that process means
mapping it to the model: the daemon's ``/api/show`` Modelfile names the weights
blob (``FROM <root>/blobs/sha256-<hex>``) and the runner's command line carries
the same blob path in ``--model``.

Everything here is slow or blocking (a PowerShell CIM query on Windows takes
about a second), so it only ever runs from ``MeasurementScheduler``'s single
background thread after a cold load, never on the request path.

Thread affinity: ``MeasurementScheduler.schedule`` may be called from any thread;
the measurement runs on one daemon thread at a time (single-flight).
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
from typing import Callable, Iterable, NamedTuple

from sonder_runtime.platform import host_memory
from sonder_runtime.platform.runtime_threads import Thread as owned_runtime_thread

logger = logging.getLogger(__name__)

_BLOB = re.compile(r"sha256[-:]([0-9a-fA-F]{64})")
_FROM_LINE = re.compile(r"^\s*FROM\s+(\S.*?)\s*$", re.IGNORECASE | re.MULTILINE)
_CIM_TIMEOUT_SECONDS = 8


class RunnerProcess(NamedTuple):
    pid: int
    command_line: str


class Measurement(NamedTuple):
    working_set_bytes: int
    private_bytes: int


def blob_digest_from_text(text: str) -> str | None:
    """The 64-hex sha256 of the first blob path in ``text``, lowercase."""
    match = _BLOB.search(str(text or ""))
    return match.group(1).lower() if match else None


def weights_blob_from_modelfile(modelfile: str) -> str | None:
    """The weights blob digest named by a ``/api/show`` Modelfile ``FROM`` line."""
    for target in _FROM_LINE.findall(str(modelfile or "")):
        digest = blob_digest_from_text(target)
        if digest is not None:
            return digest
    return None


def list_runner_processes(
    names: Iterable[str], *, runner=subprocess.run, platform_name: str | None = None,
    proc_root: str = "/proc",
) -> list[RunnerProcess]:
    """Processes named in ``names`` with their command lines; empty if unreadable."""
    wanted = {name.casefold() for name in names}
    platform_name = sys.platform if platform_name is None else platform_name
    if platform_name == "win32":
        quoted = ",".join("'%s'" % name.replace("'", "''") for name in sorted(wanted))
        script = (
            "@(Get-CimInstance Win32_Process|Where-Object{@(%s) -contains $_.Name.ToLower()}|"
            "ForEach-Object{[pscustomobject]@{ProcessId=[int]$_.ProcessId;"
            "CommandLine=[string]$_.CommandLine}})|ConvertTo-Json -Compress" % quoted
        )
        try:
            result = runner(
                ["powershell", "-NoProfile", "-Command", script],
                capture_output=True, text=True, timeout=_CIM_TIMEOUT_SECONDS, check=False,
            )
            if result.returncode != 0 or not (result.stdout or "").strip():
                return []
            decoded = json.loads(result.stdout)
        except (OSError, subprocess.SubprocessError, ValueError):
            return []
        rows = decoded if isinstance(decoded, list) else [decoded]
        return [RunnerProcess(int(row["ProcessId"]), str(row.get("CommandLine") or ""))
                for row in rows if isinstance(row, dict) and row.get("ProcessId")]
    found: list[RunnerProcess] = []
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return []
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(os.path.join(proc_root, entry, "comm"), "r", errors="replace") as handle:
                comm = handle.read().strip().casefold()
            if comm not in wanted:
                continue
            with open(os.path.join(proc_root, entry, "cmdline"), "rb") as handle:
                command = handle.read().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        found.append(RunnerProcess(int(entry), command))
    return found


def measure_runner_for_blob(
    blob_digest: str, names: Iterable[str], *,
    lister: Callable[[Iterable[str]], list[RunnerProcess]] = list_runner_processes,
    reader: Callable[[int], object] = host_memory.process_memory,
) -> Measurement | None:
    """Memory of the runner whose ``--model`` is ``blob_digest``; the largest if several."""
    best: Measurement | None = None
    for process in lister(names):
        if blob_digest_from_text(process.command_line) != blob_digest:
            continue
        memory = reader(process.pid)
        if memory is None:
            continue
        candidate = Measurement(int(memory.working_set_bytes), int(memory.private_bytes))
        if best is None or candidate.private_bytes > best.private_bytes:
            best = candidate
    return best


class MeasurementScheduler:
    """Run at most one footprint measurement at a time, off the request path."""

    def __init__(
        self, *, background: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        self._background = background or self._spawn
        self._lock = threading.Lock()
        self._inflight = False

    @staticmethod
    def _spawn(work: Callable[[], None]) -> None:
        owned_runtime_thread(target=work, daemon=True, name="sonder-gpu-footprint").start()

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._inflight

    def schedule(self, work: Callable[[], None]) -> bool:
        """Start ``work`` unless a measurement is running; returns whether it started."""
        with self._lock:
            if self._inflight:
                return False
            self._inflight = True

        def run() -> None:
            try:
                work()
            except Exception:  # a measurement must never disturb a caller
                logger.warning("host footprint measurement failed", exc_info=True)
            finally:
                with self._lock:
                    self._inflight = False

        try:
            self._background(run)
        except BaseException:
            with self._lock:
                self._inflight = False
            raise
        return True


__all__ = [
    "Measurement", "MeasurementScheduler", "RunnerProcess", "blob_digest_from_text",
    "list_runner_processes", "measure_runner_for_blob", "weights_blob_from_modelfile",
]
