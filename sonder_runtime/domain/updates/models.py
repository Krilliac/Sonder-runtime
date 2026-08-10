"""Updates domain models (SPEC-5 §21).

Wraps SPEC-4 signed-update lifecycle behind domain types:
TUF verification, backup, drain, health gates, atomic activation,
rollback, offline import, resumable download.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class UpdatePhase(Enum):
    AVAILABLE = "available"
    DOWNLOADING = "downloading"
    DOWNLOADED = "downloaded"
    VERIFYING = "verifying"
    VERIFIED = "verified"
    BACKING_UP = "backing_up"
    DRAINING = "draining"
    ACTIVATING = "activating"
    ACTIVATED = "activated"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"


TERMINAL_PHASES = frozenset({
    UpdatePhase.ACTIVATED,
    UpdatePhase.ROLLED_BACK,
    UpdatePhase.FAILED,
})


@dataclass(frozen=True)
class ReleaseMetadata:
    version: str
    channel: str = "stable"
    digest: str = ""
    size_bytes: int = 0
    release_notes: str = ""


@dataclass
class UpdateRun:
    id: str
    phase: UpdatePhase
    release: ReleaseMetadata
    backup_path: str = ""
    verified: bool = False
    health_ok: bool = False

    @property
    def is_terminal(self) -> bool:
        return self.phase in TERMINAL_PHASES

    @property
    def can_activate(self) -> bool:
        return (
            self.phase == UpdatePhase.DRAINING
            and self.verified
            and self.health_ok
        )
