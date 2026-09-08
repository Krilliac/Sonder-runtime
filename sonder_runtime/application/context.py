"""Request-scoped immutable operation context (SPEC-2 §6, SPEC-3 R-M7).

Principal, privilege, source, deadline, cancellation, workspace roots,
and consent travel together explicitly. All privileged entry points
accept this context or create one through an audited factory; the
compatibility factory below constructs the local-owner context existing
REPL calls imply.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol


class CancellationToken(Protocol):
    """Cooperative cancellation signal (implemented by the shutdown layer)."""

    @property
    def cancelled(self) -> bool: ...

    def wait(self, timeout: float | None = None) -> bool: ...


class _NeverCancelled:
    @property
    def cancelled(self) -> bool:
        return False

    def wait(self, timeout: float | None = None) -> bool:
        if timeout:
            # Match Event.wait: positive timeouts wait; negative ones return.
            time.sleep(max(timeout, 0.0))
        return False


AuthLevel = Literal["local", "user", "developer", "admin"]
Source = Literal["http", "mcp", "repl", "worker", "system"]

# The single-owner principal every surface defaults to until multi-user
# authorization exists (SPEC-3 R-M14: the seam, not the implementation).
LOCAL_OWNER = "owner"


@dataclass(frozen=True)
class OperationContext:
    correlation_id: str
    principal_id: str
    auth_level: AuthLevel
    source: Source
    deadline_monotonic: float | None
    cancellation: CancellationToken
    workspace_roots: tuple[Path, ...] = ()
    cloud_allowed: bool = False
    remote_ollama_allowed: bool = False
    # The canonical session that owns durable model/tool/job evidence.  This
    # is optional for one-shot calls, but long-running coding runs bind it so
    # subprocess jobs can be replayed with the same conversation.
    session_id: str | None = None

    @property
    def remaining_seconds(self) -> float | None:
        if self.deadline_monotonic is None:
            return None
        return max(0.0, self.deadline_monotonic - time.monotonic())

    @property
    def expired(self) -> bool:
        remaining = self.remaining_seconds
        return remaining is not None and remaining <= 0.0


def local_owner_context(
    *,
    correlation_id: str,
    source: Source = "repl",
    auth_level: AuthLevel = "local",
    workspace_roots: tuple[Path, ...] = (),
    cloud_allowed: bool = False,
    remote_ollama_allowed: bool = False,
    timeout_seconds: float | None = None,
    cancellation: CancellationToken | None = None,
    session_id: str | None = None,
) -> OperationContext:
    """Audited factory for the single-owner local context."""
    if session_id is not None and (
        not isinstance(session_id, str) or not session_id.strip()
    ):
        raise ValueError("session_id must be non-empty when supplied")
    return OperationContext(
        correlation_id=correlation_id,
        principal_id=LOCAL_OWNER,
        auth_level=auth_level,
        source=source,
        deadline_monotonic=(
            time.monotonic() + timeout_seconds
            if timeout_seconds is not None
            else None
        ),
        cancellation=cancellation or _NeverCancelled(),
        workspace_roots=workspace_roots,
        cloud_allowed=cloud_allowed,
        remote_ollama_allowed=remote_ollama_allowed,
        session_id=session_id,
    )
