"""Typed result of the host-owned current terminal eligibility decision."""
from __future__ import annotations

from dataclasses import dataclass, field


_AUTHORITY_SEAL = object()


class _HostVerifierAuthority:
    """Opaque sealed-decision authority issued only by the verifier boundary.

    The authority is deliberately process-local and cannot be serialized with
    a caller's eligibility object.  It holds a snapshot: the exact decision the
    boundary derived from the owner-bound durable turn at issue time.  It does
    not re-read the turn when evidence is consumed; consumers must check the
    issuing owner and expected turn themselves.
    """

    __slots__ = ("_resolver", "_owner")

    def __init__(self, resolver, seal, owner=None):
        if seal is not _AUTHORITY_SEAL or not callable(resolver):
            raise TypeError("managed verifier authority is private")
        self._resolver = resolver
        self._owner = owner

    def resolve(self):
        value = self._resolver()
        if type(value) is not ManagedTerminalEligibility or value.authority is None:
            raise PermissionError("current managed verifier authority is unavailable")
        return value

    def issued_by(self, owner) -> bool:
        return self._owner is not None and self._owner is owner


def _issue_host_verifier_authority(resolver, owner=None):
    return _HostVerifierAuthority(resolver, _AUTHORITY_SEAL, owner)


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
    # opaque authority sealing this exact decision (a snapshot, not a re-read).
    authority: object | None = field(default=None, repr=False, compare=False)
