"""Safe skill refresh and trust policy decisions."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class SkillTrust(StrEnum):
    BUNDLED = "bundled"
    PROJECT = "project"
    CONFIGURED = "configured"
    UNTRUSTED = "untrusted"


@dataclass(frozen=True)
class SkillRevision:
    name: str
    digest: str
    version: str
    trust: SkillTrust
    compatible: bool = True

    def refresh_allowed(self, *, expected_digest: str | None = None) -> bool:
        return self.trust is not SkillTrust.UNTRUSTED and self.compatible and (expected_digest is None or expected_digest == self.digest)


def refresh_decision(current: SkillRevision | None, candidate: SkillRevision) -> str:
    if not candidate.refresh_allowed():
        return "reject"
    if current is None:
        return "install"
    return "unchanged" if current.digest == candidate.digest else "replace"
