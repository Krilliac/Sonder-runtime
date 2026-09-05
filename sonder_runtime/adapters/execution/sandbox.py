"""Retired sandbox API: imports remain compatible, execution is unavailable.

The former adapter did not enforce its memory/network/path policy and could
silently run container requests on the host. It has no repository production
caller. Use the application-owned isolated_runner service for container work,
or an explicitly authorized durable ProcessJobProvider for host execution.
This compatibility module never starts a process or stages caller code.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from ...domain.execution.sandbox import IsolationLevel, SandboxResult

__all__ = [
    "IsolationLevel", "SandboxPolicy", "SandboxResult",
    "run_isolated", "run_python_isolated",
]


@dataclass(frozen=True)
class SandboxPolicy:
    """Legacy request data, retained for imports; no execution accepts it."""

    level: IsolationLevel = IsolationLevel.SUBPROCESS
    timeout_seconds: float = 30.0
    max_memory_mb: int = 512
    allow_network: bool = False
    allowed_paths: tuple[str, ...] = ()
    env_allowlist: tuple[str, ...] = (
        "PATH", "HOME", "LANG", "LC_ALL", "PYTHONPATH",
    )

    def effective_env(self) -> dict[str, str]:
        return {key: value for key, value in os.environ.items()
                if key in self.env_allowlist}


_UNAVAILABLE = (
    "LEGACY_SANDBOX_UNAVAILABLE: this adapter is retired because its isolation "
    "policy was not enforced. Use the application-owned isolated_runner service "
    "for container execution or an authorized durable ProcessJobProvider for "
    "intentional host execution. No command was started."
)


def run_isolated(command: list[str], *, policy: SandboxPolicy | None = None,
                 stdin_data: str = "", cwd: str | Path | None = None) -> SandboxResult:
    """Return explicit unavailability for every legacy isolation level."""
    return SandboxResult(exit_code=-1, error=_UNAVAILABLE)


def run_python_isolated(code: str, *, policy: SandboxPolicy | None = None,
                        cwd: str | Path | None = None) -> SandboxResult:
    """Return unavailability before creating any temporary script."""
    return SandboxResult(exit_code=-1, error=_UNAVAILABLE)
