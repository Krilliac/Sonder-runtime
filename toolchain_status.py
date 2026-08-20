"""Bounded version/status probes for already-discovered host tools.

This is deliberately *not* a command runner.  A caller may name only a tool
that :mod:`environment_probe` found on PATH, and every supported tool has one
fixed, argument-free status invocation.  That gives an agent a grounded way
to turn discovery into evidence without accepting a shell, executable path,
or caller-controlled arguments.
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time

import sonder_logging
from sonder_runtime.adapters import process_termination
from sonder_runtime.platform import toolchain_policy


TIMEOUT_SECONDS = 3
MAX_OUTPUT_CHARS = 2_000

# Keep this intentionally small.  A tool must have a non-interactive,
# read-only version switch before it can be probed.  Unknown tools remain
# discoverable through environment_status but are not executable here.
def _available_path(name: str, refresh: bool) -> str:
    return toolchain_policy.discovered_path(name, refresh=refresh)


def _safe_output(text: str) -> str:
    """Compatibility delegate for packaged toolchain-output policy."""
    return toolchain_policy.safe_output(text, max_chars=MAX_OUTPUT_CHARS)


def _terminate_process_tree(proc) -> None:
    """Compatibility delegate for the packaged process-teardown adapter."""
    return process_termination.terminate_process_tree(
        proc,
        os_module=os,
        signal_module=signal,
        subprocess_module=subprocess,
    )


def _run_bounded(argv: list[str]) -> tuple[str, str]:
    """Run fixed argv while retaining at most one shared pipe-output budget."""
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "env": sonder_logging.child_environment(),
        "shell": False,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(argv, **kwargs)
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
                    remaining = MAX_OUTPUT_CHARS - size
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
    deadline = time.monotonic() + TIMEOUT_SECONDS
    outcome = "ok"
    try:
        while proc.poll() is None:
            if overflow.is_set():
                outcome = "output_limit"
                _terminate_process_tree(proc)
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                outcome = "timeout"
                _terminate_process_tree(proc)
                break
            time.sleep(min(0.02, remaining))
        proc.wait(timeout=1)
    finally:
        if proc.poll() is None:
            _terminate_process_tree(proc)
            proc.wait(timeout=1)
        for reader in readers:
            reader.join(timeout=1)
    if overflow.is_set():
        outcome = "output_limit"
    elif outcome == "ok" and proc.returncode != 0:
        outcome = "error"
    return outcome, "".join(chunks)


def status(name: str, refresh: bool = False) -> dict[str, object]:
    """Run a fixed non-interactive version probe for one discovered tool."""
    tool = (name or "").strip().lower()
    arguments = toolchain_policy.allowed_arguments(tool)
    if arguments is None:
        return {
            "ok": False,
            "tool": tool,
            "error": "unsupported tool; use environment_status to inspect supported host tools",
        }
    path = _available_path(tool, refresh)
    if not path:
        return {"ok": False, "tool": tool, "error": "tool is not available on this host"}
    try:
        outcome, output = _run_bounded([path, *arguments])
    except subprocess.TimeoutExpired:
        return {"ok": False, "tool": tool, "error": "status probe timed out"}
    except OSError:
        return {"ok": False, "tool": tool, "error": "status probe could not start"}
    if outcome == "timeout":
        return {"ok": False, "tool": tool, "error": "status probe timed out"}
    if outcome == "output_limit":
        return {"ok": False, "tool": tool, "error": "status probe output exceeded limit"}
    if outcome != "ok":
        # A broken or wrapped executable controls stderr.  Do not copy its
        # arbitrary failure text into activity history; the exit verdict is
        # enough for an agent to choose a different safe path.
        return {"ok": False, "tool": tool, "error": "status probe failed"}
    return {"ok": True, "tool": tool, "output": _safe_output(output)}
