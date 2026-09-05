"""Deprecated sandbox compatibility types; they confer no execution authority.

The legacy adapter re-exports these types but always returns unavailable.
Current sandbox lifecycle contracts live in application.ports.sandbox.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class IsolationLevel(Enum):
    NONE = "none"
    SUBPROCESS = "subprocess"
    CONTAINER = "container"


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


__all__ = [
    "IsolationLevel",
    "SandboxResult",
]
