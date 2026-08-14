"""Bounded, content-free status probe for the optional sccache compiler cache."""
from __future__ import annotations

import shutil
import subprocess
import threading
import time

import sonder_logging


_TIMEOUT_SECONDS = 3
_MAX_OUTPUT_CHARS = 8_000
# Keep only operational scalars.  In particular, do not return the cache path,
# base directories, environment, or any arbitrary diagnostic text from a local
# executable to an MCP caller.
_ALLOWED_LABELS = frozenset({
    "Compile requests",
    "Compile requests executed",
    "Cache hits",
    "Cache misses",
    "Cache hits rate",
    "Cache timeouts",
    "Cache read errors",
    "Cache write errors",
    "Cache errors",
    "Compilations",
    "Compilation failures",
    "Non-cacheable compilations",
    "Non-cacheable calls",
    "Unsupported compiler calls",
    "Failed distributed compilations",
    "Version (client)",
    "Max cache size",
})


def _safe_stats(text: str) -> list[str]:
    rows = []
    for raw in str(text or "").splitlines():
        line = raw.strip()
        for label in _ALLOWED_LABELS:
            if line.startswith(label):
                value = line[len(label):].strip()
                if value:
                    rows.append("%s %s" % (label, value))
                break
    return rows


def _run_bounded(argv: list[str]) -> tuple[str, str]:
    """Run one fixed probe without accumulating unbounded pipe output.

    Both pipes are continuously drained in small chunks.  They share one
    output budget; once it is exhausted the child is terminated and no further
    text is retained.  This avoids the common ``subprocess.run(..., PIPE)``
    failure mode where a wedged diagnostic fills memory before a timeout can
    protect the server.
    """
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=sonder_logging.child_environment(),
        shell=False,
    )
    chunks: list[str] = []
    size = 0
    lock = threading.Lock()
    overflow = threading.Event()

    def drain(stream):
        nonlocal size
        try:
            while True:
                part = stream.read(1024)
                if not part:
                    return
                with lock:
                    remaining = _MAX_OUTPUT_CHARS - size
                    if remaining <= 0:
                        overflow.set()
                    else:
                        chunks.append(part[:remaining])
                        size += min(len(part), remaining)
                        if len(part) > remaining:
                            overflow.set()
        finally:
            stream.close()

    readers = [threading.Thread(target=drain, args=(stream,), daemon=True)
               for stream in (proc.stdout, proc.stderr)]
    for reader in readers:
        reader.start()
    deadline = time.monotonic() + _TIMEOUT_SECONDS
    outcome = "ok"
    try:
        while proc.poll() is None:
            if overflow.is_set():
                outcome = "output_limit"
                proc.kill()
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                outcome = "timeout"
                proc.kill()
                break
            time.sleep(min(0.02, remaining))
        proc.wait(timeout=1)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=1)
        for reader in readers:
            reader.join(timeout=1)
    if overflow.is_set():
        outcome = "output_limit"
    elif outcome == "ok" and proc.returncode != 0:
        outcome = "error"
    return outcome, "".join(chunks)


def status() -> dict:
    """Return a fixed-command, bounded, path-free sccache health snapshot."""
    executable = shutil.which("sccache")
    if not executable:
        return {"ok": True, "available": False, "status": "not_installed", "stats": []}
    try:
        outcome, combined = _run_bounded([executable, "--show-stats"])
    except subprocess.TimeoutExpired:
        return {"ok": False, "available": True, "status": "timeout", "stats": []}
    except OSError:
        return {"ok": False, "available": True, "status": "unavailable", "stats": []}
    if outcome != "ok":
        return {"ok": False, "available": True, "status": outcome, "stats": []}
    return {
        "ok": True,
        "available": True,
        "status": "ok",
        "stats": _safe_stats(combined),
    }
