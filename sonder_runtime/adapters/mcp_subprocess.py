"""Bounded lifecycle ownership for one-shot stdio MCP providers."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .process_termination import terminate_process_tree


class McpProviderTimeout(TimeoutError):
    """The provider exceeded its bounded stdio call or shutdown window."""


@dataclass(frozen=True)
class McpProviderLifecycleEvent:
    """Safe lifecycle observation; command and environment are never included."""

    state: str
    returncode: int | None = None


LifecycleObserver = Callable[[McpProviderLifecycleEvent], None]


class McpSubprocessProvider:
    """Run one MCP provider exchange with explicit, bounded ownership.

    The adapter is deliberately one-shot: a provider process handles one
    request stream and is always reaped before ``run`` returns or raises.
    This avoids pretending that a pipe can be safely reused after
    ``communicate`` closes stdin.
    """

    def __init__(
        self,
        argv: Sequence[str],
        *,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float = 30.0,
        shutdown_timeout_seconds: float = 2.0,
        observer: LifecycleObserver | None = None,
        popen=subprocess.Popen,
    ) -> None:
        if not argv or any(not isinstance(item, str) or not item for item in argv):
            raise ValueError("MCP provider argv must be non-empty strings")
        if timeout_seconds <= 0 or shutdown_timeout_seconds <= 0:
            raise ValueError("MCP provider timeouts must be positive")
        self._argv = tuple(argv)
        self._cwd = cwd
        self._env = dict(env) if env is not None else None
        self._timeout = float(timeout_seconds)
        self._shutdown_timeout = float(shutdown_timeout_seconds)
        self._observer = observer
        self._popen = popen
        self._process = None

    def run(self, request: str) -> tuple[str, str]:
        """Exchange newline-delimited MCP input and return stdout/stderr."""
        if not isinstance(request, str):
            raise TypeError("MCP provider request must be text")
        if self._process is not None:
            raise RuntimeError("MCP provider exchange is already active")
        process = self._popen(
            list(self._argv), cwd=self._cwd, env=self._env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        )
        self._process = process
        self._emit("started")
        try:
            stdout, stderr = process.communicate(request, timeout=self._timeout)
        except subprocess.TimeoutExpired as exc:
            self._terminate(process)
            self._emit("timed_out", process.returncode)
            raise McpProviderTimeout("MCP provider exceeded its bounded call") from exc
        except BaseException:
            self._terminate(process)
            self._emit("failed", process.returncode)
            raise
        finally:
            if process.poll() is None:
                self._terminate(process)
            self._process = None
        self._emit("exited", process.returncode)
        return stdout, stderr

    def close(self) -> None:
        """Terminate and reap an active provider, if one exists."""
        process = self._process
        if process is None or process.poll() is not None:
            return
        self._terminate(process)
        self._emit("closed", process.returncode)
        self._process = None

    def _terminate(self, process) -> None:
        terminate_process_tree(process)
        try:
            process.communicate(timeout=self._shutdown_timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=self._shutdown_timeout)

    def _emit(self, state: str, returncode: int | None = None) -> None:
        if self._observer is None:
            return
        try:
            self._observer(McpProviderLifecycleEvent(state, returncode))
        except Exception:
            pass


__all__ = ["McpProviderLifecycleEvent", "McpProviderTimeout", "McpSubprocessProvider"]
