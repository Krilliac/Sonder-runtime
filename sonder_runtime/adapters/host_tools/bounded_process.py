"""Bounded launch of one fixed, host-owned argv.

This is the single runner behind version probes (the host tool inventory and
the legacy ``toolchain_status`` wrapper).  It is deliberately not a command
runner: callers pass only argv built from host-owned constants.

Bounds: no shell, stdin closed, a shared stdout+stderr character budget, a
wall-clock timeout, and whole process-tree termination (POSIX process group
or Windows ``taskkill /T``) when either bound is exceeded.

Working directory: unless a caller passes ``cwd``, the child starts in a
neutral, administrator-owned directory (``/`` on POSIX, ``%SystemRoot%`` on
Windows) rather than inheriting the server's working directory.  Many
toolchains read configuration from the current directory and its ancestors
before printing a version -- ``go`` honours a ``go.mod`` ``toolchain`` line
(download and exec), ``yarn`` a ``.yarnrc`` ``yarnPath``, rustup proxies a
``rust-toolchain.toml``, Maven ``.mvn/jvm.config``, and a Windows ``.cmd``
shim resolves bare commands from the current directory first.  Inheriting a
project checkout as the working directory would let project files choose what
a "version probe" actually runs.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import signal
import subprocess
import threading
import time
from typing import Mapping, Sequence

import sonder_runtime.adapters.process_termination as process_termination
import sonder_runtime.platform.logging as runtime_logging
import sonder_runtime.platform.runtime_threads as runtime_threads

OUTCOMES = ("ok", "error", "timeout", "output_limit", "start_failed")
_POLL_SECONDS = 0.02
_READ_CHUNK = 1024


def neutral_cwd(os_module=os) -> str | None:
    """An administrator-owned directory with no project configuration.

    POSIX: ``/``.  Windows: ``%SystemRoot%`` (normally ``C:\\Windows``) when it
    is an absolute existing directory.  ``None`` (inherit) only when neither is
    available, which happens only under a simulated ``os`` in tests.
    """
    if os_module.name != "nt":
        return "/" if os.name != "nt" else None
    root = os.environ.get("SystemRoot") or os.environ.get("windir") or ""
    if root and os.path.isabs(root) and os.path.isdir(root):
        return root
    return None


@dataclass(frozen=True)
class BoundedRun:
    outcome: str
    output: str
    exit_code: int | None
    elapsed_ms: int


def run_bounded(
    argv: Sequence[str],
    *,
    timeout_seconds: float = 3.0,
    max_output_chars: int = 2_000,
    env: Mapping[str, str] | None = None,
    cwd: str | None = None,
    subprocess_module=subprocess,
    os_module=os,
) -> BoundedRun:
    """Run fixed argv under output and time bounds; never raises for launch."""
    if not argv or any(not isinstance(item, str) or "\x00" in item for item in argv):
        raise ValueError("argv must be non-empty strings without NUL")
    if timeout_seconds <= 0 or max_output_chars <= 0:
        raise ValueError("bounds must be positive")
    started = time.monotonic()
    kwargs = {
        "stdin": subprocess_module.DEVNULL,
        "stdout": subprocess_module.PIPE,
        "stderr": subprocess_module.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "env": dict(env) if env is not None else runtime_logging.child_environment(),
        "shell": False,
        "close_fds": True,
    }
    workdir = cwd if cwd is not None else neutral_cwd(os_module)
    if workdir is not None:
        kwargs["cwd"] = workdir
    if os_module.name == "nt":
        kwargs["creationflags"] = getattr(subprocess_module, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess_module.Popen(list(argv), **kwargs)
    except (OSError, ValueError):
        return BoundedRun("start_failed", "", None, int((time.monotonic() - started) * 1000))

    chunks: list[str] = []
    size = 0
    lock = threading.Lock()
    overflow = threading.Event()

    def drain(stream):
        nonlocal size
        try:
            while True:
                part = stream.read(_READ_CHUNK)
                if not part:
                    return
                with lock:
                    remaining = max_output_chars - size
                    if remaining <= 0:
                        overflow.set()
                    else:
                        chunks.append(part[:remaining])
                        size += min(len(part), remaining)
                        if len(part) > remaining:
                            overflow.set()
        except (OSError, ValueError):
            return
        finally:
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    def terminate():
        process_termination.terminate_process_tree(
            proc,
            os_module=os_module,
            signal_module=signal,
            subprocess_module=subprocess_module,
        )

    readers = []
    for stream in (proc.stdout, proc.stderr):
        if stream is None:
            continue
        reader = runtime_threads.Thread(
            target=drain, args=(stream,), daemon=True, name="sonder-bounded-probe-drain",
        )
        readers.append(reader)
    for reader in readers:
        reader.start()
    deadline = started + timeout_seconds
    outcome = "ok"
    try:
        while proc.poll() is None:
            if overflow.is_set():
                outcome = "output_limit"
                terminate()
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                outcome = "timeout"
                terminate()
                break
            time.sleep(min(_POLL_SECONDS, remaining))
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
    finally:
        if proc.poll() is None:
            terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        for reader in readers:
            reader.join(timeout=1)
    if overflow.is_set():
        outcome = "output_limit"
    exit_code = proc.returncode
    if outcome == "ok" and exit_code != 0:
        outcome = "error"
    with lock:
        output = "".join(chunks)
    return BoundedRun(outcome, output, exit_code, int((time.monotonic() - started) * 1000))


__all__ = ["BoundedRun", "OUTCOMES", "neutral_cwd", "run_bounded"]
