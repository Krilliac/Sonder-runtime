"""Typed result of the host-owned current terminal eligibility decision."""
from __future__ import annotations

from dataclasses import dataclass, field


_AUTHORITY_SEAL = object()


class _HostVerifierAuthority:
    """Opaque live resolver issued only by the managed verifier boundary.

    The resolver is deliberately process-local.  It cannot be serialized with
    a caller's eligibility object and re-reads the current durable host turn
    when evidence is consumed.
    """

    __slots__ = ("_resolver",)

    def __init__(self, resolver, seal):
        if seal is not _AUTHORITY_SEAL or not callable(resolver):
            raise TypeError("managed verifier authority is private")
        self._resolver = resolver

    def resolve(self):
        value = self._resolver()
        if type(value) is not ManagedTerminalEligibility or value.authority is None:
            raise PermissionError("current managed verifier authority is unavailable")
        return value


def _issue_host_verifier_authority(resolver):
    return _HostVerifierAuthority(resolver, _AUTHORITY_SEAL)


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
    verified_failure_receipt: object | None = None
    # Never accepted from a public caller.  The managed verifier attaches an
    # opaque resolver which re-reads the owner-bound durable turn.
    authority: object | None = field(default=None, repr=False, compare=False)
