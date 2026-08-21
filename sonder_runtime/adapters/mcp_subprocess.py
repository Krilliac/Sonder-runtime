"""Bounded lifecycle ownership for one-shot stdio MCP providers."""
from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from ..application.jobs.durable_registry import (
    ProcessTreeCleanupContract,
    ProcessTreeCleanupReceipt,
    ProcessTreeCleanupRequest,
)
from ..application.protocol.mcp_compatibility import LegacyMcpContract
from .process_termination import ProcessTreeSupervisor
from .process_termination import terminate_process_tree


class McpProviderTimeout(TimeoutError):
    """The provider exceeded its bounded stdio call or shutdown window."""


class McpProviderCancelled(InterruptedError):
    """The caller cancelled a bounded provider exchange."""


@dataclass(frozen=True)
class McpProviderLifecycleEvent:
    """Safe lifecycle observation; command and environment are never included."""

    state: str
    returncode: int | None = None
    cleanup_receipt: ProcessTreeCleanupReceipt | None = None


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
        declaration: LegacyMcpContract | None = None,
        cleanup: ProcessTreeCleanupContract | None = None,
        platform_name: str | None = None,
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
        if declaration is not None and not isinstance(declaration, LegacyMcpContract):
            raise TypeError("MCP provider declaration must be a LegacyMcpContract")
        self._declaration = declaration
        self._platform = platform_name or __import__("os").name
        self._cleanup_provided = cleanup is not None
        self._cleanup = cleanup or ProcessTreeSupervisor(
            platform_name=self._platform, timeout_seconds=self._shutdown_timeout
        )
        self._observer = observer
        self._popen = popen
        self._process = None
        self._cleanup_receipt: ProcessTreeCleanupReceipt | None = None

    @property
    def declaration(self) -> LegacyMcpContract | None:
        return self._declaration

    @property
    def cleanup_receipt(self) -> ProcessTreeCleanupReceipt | None:
        """The last termination receipt, including an incomplete receipt."""
        return self._cleanup_receipt

    def run(
        self, request: str, *, cancel_check: Callable[[], bool] | None = None,
        deadline_monotonic: float | None = None,
    ) -> tuple[str, str]:
        """Exchange newline-delimited MCP input and return stdout/stderr."""
        if not isinstance(request, str):
            raise TypeError("MCP provider request must be text")
        if self._process is not None:
            raise RuntimeError("MCP provider exchange is already active")
        process = self._popen(
            list(self._argv), cwd=self._cwd, env=self._env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
            **({"start_new_session": True} if self._platform == "posix" else {}) ,
            **({"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)} if self._platform == "nt" else {}),
        )
        self._process = process
        self._emit("started")
        result: list[object] = []

        def communicate() -> None:
            try:
                result.append(process.communicate(request))
            except BaseException as exc:
                result.append(exc)

        worker = threading.Thread(target=communicate, name="sonder-mcp-provider", daemon=True)
        worker.start()
        termination_requested = False
        call_deadline = time.monotonic() + self._timeout
        try:
            while True:
                if cancel_check is not None and cancel_check():
                    termination_requested = True
                    receipt = self._terminate(process, "provider cancellation", reap=False)
                    worker.join(self._shutdown_timeout)
                    self._emit("cancelled", process.returncode, receipt)
                    raise McpProviderCancelled("MCP provider exchange was cancelled")
                remaining = call_deadline - time.monotonic()
                if deadline_monotonic is not None:
                    remaining = min(remaining, deadline_monotonic - time.monotonic())
                if remaining <= 0:
                    termination_requested = True
                    receipt = self._terminate(process, "provider deadline", reap=False)
                    worker.join(self._shutdown_timeout)
                    self._emit("timed_out", process.returncode, receipt)
                    raise McpProviderTimeout("MCP provider exceeded its bounded call deadline")
                worker.join(min(remaining, 0.05))
                if not worker.is_alive():
                    break
            if not result:
                raise RuntimeError("MCP provider communication ended without a result")
            if isinstance(result[0], BaseException):
                raise result[0]
            stdout, stderr = result[0]
        except BaseException:
            if process.poll() is None and not termination_requested:
                termination_requested = True
                receipt = self._terminate(process, "provider failure", reap=False)
                worker.join(self._shutdown_timeout)
                self._emit("failed", process.returncode, receipt)
            raise
        finally:
            if process.poll() is None and not termination_requested:
                self._terminate(process, "provider finalization", reap=False)
                worker.join(self._shutdown_timeout)
            self._process = None
        self._emit("exited", process.returncode)
        return stdout, stderr

    def close(self) -> None:
        """Terminate and reap an active provider, if one exists."""
        process = self._process
        if process is None or process.poll() is not None:
            return
        receipt = self._terminate(process, "provider closed")
        self._emit("closed", process.returncode, receipt)
        self._process = None

    def _terminate(self, process, reason: str, *, reap: bool = True) -> ProcessTreeCleanupReceipt:
        if not reap:
            # Deadline/cancellation owns a hard bound.  Stop the direct child
            # first; the receipt below still reports whether tree cleanup was
            # actually proven by the platform supervisor.
            try:
                process.kill()
            except OSError:
                pass
        request = ProcessTreeCleanupRequest(
            job_id=f"mcp-provider:{getattr(process, 'pid', 0)}",
            process_id=getattr(process, "pid", 0),
            process_group_id=getattr(process, "pid", None) if self._platform == "posix" else None,
            reason=reason,
        )
        if not reap and not self._cleanup_provided:
            receipt = ProcessTreeCleanupReceipt(
                request.job_id, True, complete=False,
                detail="direct child stopped before bounded tree cleanup verification",
            )
        else:
            try:
                receipt = self._cleanup.cleanup(request)
            except Exception as exc:
                receipt = ProcessTreeCleanupReceipt(
                    request.job_id, True, complete=False,
                    detail=f"cleanup adapter failed: {type(exc).__name__}",
                )
        if not receipt.complete:
            # The receipt stays incomplete; this fallback is only a safety
            # attempt and never upgrades the claim made by the receipt.
            try:
                process.kill()
            except OSError:
                pass
        self._cleanup_receipt = receipt
        if reap:
            try:
                process.communicate(timeout=self._shutdown_timeout)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                    process.communicate(timeout=self._shutdown_timeout)
                except subprocess.TimeoutExpired:
                    self._cleanup_receipt = ProcessTreeCleanupReceipt(
                        request.job_id, True, complete=False,
                        detail="provider did not reap before shutdown deadline",
                    )
        return self._cleanup_receipt

    def _emit(self, state: str, returncode: int | None = None, receipt=None) -> None:
        if self._observer is None:
            return
        try:
            self._observer(McpProviderLifecycleEvent(state, returncode, receipt))
        except Exception:
            pass


__all__ = ["McpProviderCancelled", "McpProviderLifecycleEvent", "McpProviderTimeout", "McpSubprocessProvider"]
