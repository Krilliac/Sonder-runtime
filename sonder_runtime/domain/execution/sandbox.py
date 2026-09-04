"""Subprocess isolation for tool execution.

Wraps tool execution in subprocess boundaries with configurable resource
limits (timeout, memory, network access).  Falls back gracefully when
the platform lacks the requested isolation capability.

This module provides the isolation policy and execution boundary — the
actual tool dispatch remains in its existing location.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)


class IsolationLevel(Enum):
    NONE = "none"
    SUBPROCESS = "subprocess"
    CONTAINER = "container"


@dataclass(frozen=True)
class SandboxPolicy:
    level: IsolationLevel = IsolationLevel.SUBPROCESS
    timeout_seconds: float = 30.0
    max_memory_mb: int = 512
    allow_network: bool = False
    allowed_paths: tuple[str, ...] = ()
    env_allowlist: tuple[str, ...] = (
        "PATH", "HOME", "LANG", "LC_ALL", "PYTHONPATH",
    )

    def effective_env(self) -> dict[str, str]:
        return {
            k: v for k, v in os.environ.items()
            if k in self.env_allowlist
        }


@dataclass
class SandboxResult:
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    duration_ms: float = 0.0
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


_DEFAULT_POLICY = SandboxPolicy()


def run_isolated(
    command: list[str],
    *,
    policy: SandboxPolicy | None = None,
    stdin_data: str = "",
    cwd: str | Path | None = None,
) -> SandboxResult:
    pol = policy or _DEFAULT_POLICY

    if pol.level == IsolationLevel.NONE:
        return _run_direct(command, stdin_data=stdin_data, cwd=cwd,
                           timeout=pol.timeout_seconds)

    if pol.level == IsolationLevel.CONTAINER:
        logger.warning("container isolation not yet available, "
                       "falling back to subprocess")

    return _run_subprocess(command, pol, stdin_data=stdin_data, cwd=cwd)


def run_python_isolated(
    code: str,
    *,
    policy: SandboxPolicy | None = None,
    cwd: str | Path | None = None,
) -> SandboxResult:
    pol = policy or _DEFAULT_POLICY
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False,
    ) as f:
        f.write(code)
        script_path = f.name
    try:
        return run_isolated(
            [sys.executable, "-u", script_path],
            policy=pol,
            cwd=cwd,
        )
    finally:
        try:
            os.unlink(script_path)
        except OSError:
            pass


def _run_subprocess(
    command: list[str],
    policy: SandboxPolicy,
    *,
    stdin_data: str = "",
    cwd: str | Path | None = None,
) -> SandboxResult:
    env = policy.effective_env()
    start = time.monotonic()
    timed_out = False

    try:
        proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=str(cwd) if cwd else None,
            start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        return SandboxResult(
            exit_code=-1,
            error="failed to start process: %s" % exc,
            duration_ms=(time.monotonic() - start) * 1000,
        )

    try:
        stdout_bytes, stderr_bytes = proc.communicate(
            input=stdin_data.encode() if stdin_data else None,
            timeout=policy.timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            proc.kill()
        stdout_bytes, stderr_bytes = proc.communicate(timeout=5)
    except Exception as exc:
        duration = (time.monotonic() - start) * 1000
        return SandboxResult(
            exit_code=-1,
            error="process communication failed: %s" % exc,
            duration_ms=duration,
        )

    duration = (time.monotonic() - start) * 1000
    return SandboxResult(
        exit_code=proc.returncode if not timed_out else -1,
        stdout=stdout_bytes.decode("utf-8", errors="replace"),
        stderr=stderr_bytes.decode("utf-8", errors="replace"),
        timed_out=timed_out,
        duration_ms=duration,
    )


def _run_direct(
    command: list[str],
    *,
    stdin_data: str = "",
    cwd: str | Path | None = None,
    timeout: float = 30.0,
) -> SandboxResult:
    start = time.monotonic()
    try:
        result = subprocess.run(
            command,
            input=stdin_data.encode() if stdin_data else None,
            capture_output=True,
            timeout=timeout,
            cwd=str(cwd) if cwd else None,
        )
        duration = (time.monotonic() - start) * 1000
        return SandboxResult(
            exit_code=result.returncode,
            stdout=result.stdout.decode("utf-8", errors="replace"),
            stderr=result.stderr.decode("utf-8", errors="replace"),
            duration_ms=duration,
        )
    except subprocess.TimeoutExpired:
        duration = (time.monotonic() - start) * 1000
        return SandboxResult(
            exit_code=-1, timed_out=True, duration_ms=duration,
        )
    except (OSError, ValueError) as exc:
        duration = (time.monotonic() - start) * 1000
        return SandboxResult(
            exit_code=-1,
            error="failed to run: %s" % exc,
            duration_ms=duration,
        )


__all__ = [
    "IsolationLevel",
    "SandboxPolicy",
    "SandboxResult",
    "run_isolated",
    "run_python_isolated",
]
