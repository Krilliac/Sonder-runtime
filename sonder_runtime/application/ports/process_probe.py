"""Process liveness port for durable-task ownership (SPEC-3 section 4)."""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    started_at: float
    fingerprint: str = ""


class ProbeResult(enum.Enum):
    ALIVE = "alive"
    DEAD = "dead"
    UNKNOWN = "unknown"  # unknown liveness must never cause split-brain claims


class ProcessProbe(Protocol):
    def identity(self, pid: int) -> ProcessIdentity | None: ...

    def is_same_live_process(self, identity: ProcessIdentity) -> ProbeResult: ...
