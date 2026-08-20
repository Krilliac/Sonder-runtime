"""Pure normalization helpers used by host hardware probes."""

from __future__ import annotations

import platform
import re
import os


def parse_memory_gb(value: object) -> float | None:
    """Parse a human-readable MB/GB memory value into gigabytes."""
    text = str(value or "").strip().lower().replace(",", ".")
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(gb|mb)", text)
    if not match:
        return None
    amount = float(match.group(1))
    return amount if match.group(2) == "gb" else amount / 1024.0


def probe_platform() -> str:
    """Return the normalized host platform name without raising."""
    try:
        return platform.system() or "unknown"
    except Exception:
        return "unknown"


def probe_cpu_count() -> int | None:
    """Return the host CPU count without allowing probe failures to escape."""
    try:
        return os.cpu_count()
    except Exception:
        return None
