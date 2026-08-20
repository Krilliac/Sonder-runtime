"""Bounded process-tree teardown for adapters that own child processes."""
from __future__ import annotations

import os
import signal
import subprocess


def terminate_process_tree(
    proc,
    *,
    os_module=os,
    signal_module=signal,
    subprocess_module=subprocess,
) -> None:
    """Best-effort teardown of a process and its ordinary descendants.

    The optional modules keep the compatibility wrapper testable without
    coupling this adapter to the legacy module's imports.
    """
    if proc.poll() is not None:
        return
    pid = getattr(proc, "pid", None)
    if os_module.name == "nt" and pid:
        try:
            subprocess_module.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdin=subprocess_module.DEVNULL,
                stdout=subprocess_module.DEVNULL,
                stderr=subprocess_module.DEVNULL,
                timeout=5,
                check=False,
                shell=False,
            )
            return
        except (OSError, subprocess_module.SubprocessError):
            pass
    elif pid:
        try:
            os_module.killpg(pid, signal_module.SIGKILL)
            return
        except (OSError, ProcessLookupError):
            pass
    try:
        proc.kill()
    except OSError:
        pass


__all__ = ["terminate_process_tree"]
