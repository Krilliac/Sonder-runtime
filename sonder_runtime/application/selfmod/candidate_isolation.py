"""Typed isolation attestation shared by the selfmod candidate supervisors.

Two supervisors can run unattended selfmod candidate checks:

* ``scripts/selfmod_low_integrity.py`` (Windows): a low-integrity token inside
  a Job object.  Its report carries ``integrity: "low"``.
* ``scripts/selfmod_linux_isolation.py`` (Linux): a dedicated unprivileged
  uid with ``no_new_privs`` and uid-scoped teardown.  Its report carries
  ``integrity: "linux-uid"`` plus the candidate and supervisor uids.

Both reports are plain dictionaries built by the supervising process from
the kernel's view of the candidate, never from candidate stdout.  This module
turns such a report into one immutable, validated value so every consumer
(``selfmod._record_command``, the nightly parent-scored gate) applies the
same rules instead of re-implementing string checks.

The attestation says which OS boundary bounded the candidate's *writes*. It
does not claim result independence: the candidate still produces the output
the parent grades.  Independence is the separate oracle receipt in
``independent_oracle.py`` (see REMAINING-SELFMOD-517-LINUX-ISOLATION.md).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

LOW_INTEGRITY = "low"
LINUX_UID = "linux-uid"
# Every attestation kind a candidate supervisor may build.  A consumer must
# still bind a recorded kind to the supervisor the host actually selected;
# membership here alone never makes one supervisor vouch for another.
ISOLATION_KINDS = frozenset({LOW_INTEGRITY, LINUX_UID})


class IsolationAttestationError(ValueError):
    """A supervisor report does not prove the expected isolation boundary."""


def _positive_int(value: object) -> int | None:
    if type(value) is int and value > 0:
        return value
    return None


@dataclass(frozen=True, slots=True)
class IsolationAttestation:
    """A verified statement that one candidate check ran under an OS boundary.

    Construct it only with :meth:`from_supervisor_result`; the constructor
    re-validates the invariants so a hand-built value cannot claim a
    ``linux-uid`` boundary without a distinct unprivileged candidate uid.
    """

    kind: str
    exit_code: int
    passed: bool
    supervisor_uid: int | None = None
    candidate_uid: int | None = None
    candidate_gid: int | None = None
    timed_out: bool = False
    limit_hit: str | None = None
    integrity_failed: bool = False

    def __post_init__(self) -> None:
        if self.kind not in ISOLATION_KINDS:
            raise IsolationAttestationError(f"unknown isolation kind {self.kind!r}")
        if type(self.exit_code) is not int:
            raise IsolationAttestationError("attested exit code must be an integer")
        if self.passed is not (self.exit_code == 0 and not self.integrity_failed):
            raise IsolationAttestationError("attested pass does not match the exit status")
        if self.kind == LINUX_UID:
            if _positive_int(self.candidate_uid) is None:
                raise IsolationAttestationError("linux-uid attestation lacks an unprivileged candidate uid")
            if type(self.supervisor_uid) is not int or self.supervisor_uid < 0:
                raise IsolationAttestationError("linux-uid attestation lacks the supervisor uid")
            if self.candidate_uid == self.supervisor_uid:
                raise IsolationAttestationError("candidate uid equals the supervisor uid")
            if self.candidate_gid is not None and _positive_int(self.candidate_gid) is None:
                raise IsolationAttestationError("linux-uid attestation carries a privileged gid")

    @classmethod
    def from_supervisor_result(
        cls,
        result: Mapping[str, Any],
        *,
        expected_kind: str,
        supervisor_uid: int | None,
    ) -> "IsolationAttestation":
        """Validate one supervisor ``run_isolated`` result.

        ``expected_kind`` is the attestation the host-selected supervisor
        builds and ``supervisor_uid`` is this process's effective uid (``None``
        where the platform has no uids).  The report must come from that
        supervisor: a ``low`` report cannot satisfy a Linux selection and a
        ``linux-uid`` report cannot satisfy a Windows one.
        """
        if expected_kind not in ISOLATION_KINDS:
            raise IsolationAttestationError(f"unknown isolation kind {expected_kind!r}")
        if not isinstance(result, Mapping):
            raise IsolationAttestationError("supervisor result is not a mapping")
        job = result.get("job")
        if not isinstance(job, Mapping) or job.get("integrity") != expected_kind:
            raise IsolationAttestationError("supervisor report lacks the selected attestation")
        try:
            exit_code = int(result["exit_code"])
        except (KeyError, TypeError, ValueError) as exc:
            raise IsolationAttestationError("supervisor result lacks an exit code") from exc
        integrity_failed = bool(result.get("integrity_failed"))
        passed = result.get("passed")
        if passed is not (exit_code == 0) and not integrity_failed:
            raise IsolationAttestationError("supervisor pass flag contradicts its exit code")
        if integrity_failed and passed is not False:
            raise IsolationAttestationError("integrity failure reported as a pass")
        candidate_uid = candidate_gid = None
        if expected_kind == LINUX_UID:
            candidate_uid = job.get("uid")
            candidate_gid = job.get("gid")
            reported_supervisor = job.get("supervisor_uid")
            if reported_supervisor is not None and reported_supervisor != supervisor_uid:
                raise IsolationAttestationError("report was not built by this supervisor uid")
        limit_hit = job.get("limit_hit")
        return cls(
            kind=expected_kind,
            exit_code=exit_code,
            passed=exit_code == 0 and not integrity_failed,
            supervisor_uid=supervisor_uid,
            candidate_uid=candidate_uid,
            candidate_gid=candidate_gid,
            timed_out=bool(job.get("timed_out")),
            limit_hit=str(limit_hit) if limit_hit else None,
            integrity_failed=integrity_failed,
        )

    def as_record(self) -> dict[str, object]:
        """A JSON-safe summary for audit output (never parsed back as proof)."""
        return {
            "kind": self.kind,
            "exit_code": self.exit_code,
            "passed": self.passed,
            "supervisor_uid": self.supervisor_uid,
            "candidate_uid": self.candidate_uid,
            "candidate_gid": self.candidate_gid,
            "timed_out": self.timed_out,
            "limit_hit": self.limit_hit,
            "integrity_failed": self.integrity_failed,
        }


def accepted_probe_attestation(
    probe: Mapping[str, Any], *, selected_kind: str,
) -> IsolationAttestation | None:
    """Return the verified attestation of a passing candidate probe, or ``None``.

    A probe counts only when its recorded isolation equals the attestation
    of the supervisor the host selected, and the in-process typed attestation
    that ``selfmod._record_command`` built agrees with it and passed.
    """
    if not isinstance(probe, Mapping) or selected_kind not in ISOLATION_KINDS:
        return None
    attestation = probe.get("attestation")
    if (probe.get("isolation") != selected_kind
            or not isinstance(attestation, IsolationAttestation)
            or attestation.kind != selected_kind
            or not attestation.passed or probe.get("passed") is not True):
        return None
    return attestation


__all__ = [
    "ISOLATION_KINDS",
    "IsolationAttestation",
    "IsolationAttestationError",
    "LINUX_UID",
    "LOW_INTEGRITY",
    "accepted_probe_attestation",
]
