"""Typed result of the host-owned current terminal eligibility decision."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ManagedTerminalEligibility:
    """A current host decision; the evidence remains bound to its owner."""

    evidence: object
    eligible: bool
    phase: str
    code: str
    pending_identity: object | None = None
    pending_approval: object | None = None
    published: object | None = None
    authenticated_worker_id: str | None = None
    verified_subject_digest: str | None = None
