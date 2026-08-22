"""Process-backed GPU probes used by the hardware compatibility surface."""

from __future__ import annotations

import subprocess


def probe_nvidia_gpu() -> tuple[bool, float | None]:
    """Return ``(gpu_present, vram_gb)`` from a bounded NVIDIA probe."""
    out = None
    for timeout_s in (8.0, 8.0):
        try:
            out = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=memory.total",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
            break
        except subprocess.TimeoutExpired:
            continue
        except Exception:
            return (False, None)
    if out is None or out.returncode != 0:
        return (False, None)
    best_mib = 0.0
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            best_mib = max(best_mib, float(line))
        except ValueError:
            continue
    if best_mib <= 0:
        return (False, None)
    return (True, round(best_mib / 1024.0, 1))


__all__ = ["probe_nvidia_gpu"]
