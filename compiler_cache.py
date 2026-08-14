"""Bounded, content-free status probe for the optional sccache compiler cache."""
from __future__ import annotations

import shutil
import subprocess

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


def status() -> dict:
    """Return a fixed-command, bounded, path-free sccache health snapshot."""
    executable = shutil.which("sccache")
    if not executable:
        return {"ok": True, "available": False, "status": "not_installed", "stats": []}
    try:
        proc = subprocess.run(
            [executable, "--show-stats"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_TIMEOUT_SECONDS,
            env=sonder_logging.child_environment(),
            shell=False,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "available": True, "status": "timeout", "stats": []}
    except OSError:
        return {"ok": False, "available": True, "status": "unavailable", "stats": []}
    combined = ((proc.stdout or "") + "\n" + (proc.stderr or ""))[:_MAX_OUTPUT_CHARS]
    return {
        "ok": proc.returncode == 0,
        "available": True,
        "status": "ok" if proc.returncode == 0 else "error",
        "stats": _safe_stats(combined),
    }
