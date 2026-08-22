"""Truthful recovery-boundary decisions for same-user self-mod recovery.

Recovery metadata and audit files are useful for accountability, but they do
not become a security boundary merely because they are written by the same
user.  In particular, an explicitly unrestricted self-mod actor can alter or
remove both the recovery state and the audit trail.  This module makes that
limitation a typed, testable result instead of an implicit security claim.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class RecoveryBoundaryKind(str, Enum):
    SAME_USER = "same_user"
    EXTERNAL = "external"


@dataclass(frozen=True, slots=True)
class RecoveryBoundaryAssessment:
    """The authority and limitations of one recovery attempt."""

    actor: str
    resource_owner: str
    kind: RecoveryBoundaryKind
    unrestricted_selfmod: bool
    audit_files: tuple[str, ...] = ()
    security_boundary: bool = False
    limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.actor or not self.resource_owner:
            raise ValueError("actor and resource_owner are required")
        if any(not path or "\x00" in path for path in self.audit_files):
            raise ValueError("audit file paths must be non-empty and NUL-free")
        if self.security_boundary:
            raise ValueError(
                "same-user recovery and audit files cannot be declared a "
                "security boundary"
            )
        if not self.limitations:
            raise ValueError("a recovery assessment must state its limitations")


class RecoveryBoundary:
    """Create explicit, non-overclaiming recovery assessments."""

    _SAME_USER_LIMITATION = (
        "same-user recovery is an operational continuity aid, not a security "
        "boundary"
    )
    _AUDIT_LIMITATION = (
        "audit files are evidence only and are not tamper-resistant against "
        "the recovery actor"
    )
    _UNRESTRICTED_LIMITATION = (
        "explicitly unrestricted selfmod can alter recovery state and audit "
        "files; external enforcement is required for security"
    )

    @classmethod
    def assess(
        cls,
        *,
        actor: str,
        resource_owner: str,
        unrestricted_selfmod: bool = False,
        audit_files: tuple[str, ...] = (),
    ) -> RecoveryBoundaryAssessment:
        if not isinstance(unrestricted_selfmod, bool):
            raise TypeError("unrestricted_selfmod must be bool")
        same_user = actor == resource_owner
        limitations = [cls._SAME_USER_LIMITATION]
        if audit_files:
            limitations.append(cls._AUDIT_LIMITATION)
        if unrestricted_selfmod:
            limitations.append(cls._UNRESTRICTED_LIMITATION)
        if not same_user:
            limitations.append(
                "different-user recovery requires an independently enforced "
                "authorization boundary"
            )
        return RecoveryBoundaryAssessment(
            actor=actor,
            resource_owner=resource_owner,
            kind=(RecoveryBoundaryKind.SAME_USER if same_user else RecoveryBoundaryKind.EXTERNAL),
            unrestricted_selfmod=unrestricted_selfmod,
            audit_files=tuple(audit_files),
            security_boundary=False,
            limitations=tuple(limitations),
        )

    @classmethod
    def can_claim_security_boundary(cls, assessment: RecoveryBoundaryAssessment) -> bool:
        """Always return false for this same-user/audit-only contract."""
        if not isinstance(assessment, RecoveryBoundaryAssessment):
            raise TypeError("assessment must be a RecoveryBoundaryAssessment")
        return False

    @classmethod
    def recovery_notice(cls, assessment: RecoveryBoundaryAssessment) -> str:
        if not isinstance(assessment, RecoveryBoundaryAssessment):
            raise TypeError("assessment must be a RecoveryBoundaryAssessment")
        return "; ".join(assessment.limitations)


__all__ = [
    "RecoveryBoundary",
    "RecoveryBoundaryAssessment",
    "RecoveryBoundaryKind",
]
