"""Private current terminal decision; returned values confer no authority."""

from dataclasses import dataclass
from ..application.ports.host_turn_links import ManagedHostFinalEvidence
from ..application.ports.lane_continuation import (
    PendingApprovalEvidence,
    PendingVerificationIdentity,
)
from ..application.agents.host_turns import require_host_pending_turn
from .standalone_continuation import PublishedHostTerminal


@dataclass(frozen=True)
class ManagedTerminalEligibility:
    evidence: ManagedHostFinalEvidence
    eligible: bool
    phase: str
    code: str
    pending_identity: PendingVerificationIdentity | None = None
    pending_approval: PendingApprovalEvidence | None = None
    published: PublishedHostTerminal | None = None


def terminal_eligibility(session, expected_turn, *, verifier_factory):
    session.require_current()
    evidence = session.final_evidence(expected_turn)
    facts = evidence.facts
    bound = session._bound
    identity = bound.pending_verification()
    if identity is None:
        blank = (
            not facts.certificate_id
            and not facts.certificate_code
            and facts.certificate_generation == 0
        )
        if facts.delegated_work is False and blank:
            return ManagedTerminalEligibility(
                evidence, True, "not_required", "NO_DELEGATED_WORK"
            )
        refused = (
            facts.delegated_work is True
            and facts.validation_passed is False
            and facts.terminal_class
            in (
                "ERROR",
                "EVIDENCE_REQUIRED",
                "VALIDATION_FAILED",
                "CANCELLED",
                "UNVERIFIED",
            )
            and not facts.certificate_id
            and facts.certificate_generation == 0
            and facts.certificate_code
            in {"ORIGINAL_PARENT_EVIDENCE_FAILED", "VERIFICATION_UNAVAILABLE"}
        )
        return ManagedTerminalEligibility(
            evidence,
            bool(refused),
            "refused" if refused else "unknown",
            facts.certificate_code if refused else "DELEGATION_EVIDENCE_UNAVAILABLE",
        )
    require_host_pending_turn(bound, expected_turn, identity)
    if facts.delegated_work is not True:
        return ManagedTerminalEligibility(
            evidence, False, "unknown", "DELEGATION_EVIDENCE_MISMATCH", identity
        )
    session._compose_verifier(verifier_factory)
    view = bound.verification_view(
        session._verifier, identity.verification_id, action="inspect"
    )
    phase, code = view["state"], view.get("code", "")
    pending = (
        PendingApprovalEvidence(**view["pending_approval"])
        if phase == "approval_pending"
        else None
    )
    if phase != "certified":
        return ManagedTerminalEligibility(
            evidence, False, phase, code, identity, pending
        )
    verdict = bound.verification_view(
        session._verifier, identity.verification_id, action="validate"
    )
    prepared = bound.prepared_verification(identity)
    if (
        verdict.valid is not True
        or verdict.code != "CERTIFIED"
        or verdict.certificate_id != identity.verification_id
        or verdict.generation != identity.generation
        or verdict.parent_session_id != prepared.parent_session_id
        or verdict.parent_grant_revision != prepared.parent_grant_revision
        or verdict.children != prepared.children
        or verdict.roots != prepared.roots
    ):
        return ManagedTerminalEligibility(
            evidence, False, "unknown", "CERTIFICATE_NOT_CURRENT", identity
        )
    original_certified = not (
        facts.certificate_id != verdict.certificate_id
        or facts.certificate_generation != verdict.generation
        or facts.certificate_code != verdict.code
        or facts.validation_passed is not True
    )
    certified_after_return = (
        facts.validation_passed is False
        and facts.terminal_class == "UNVERIFIED"
        and not facts.certificate_id
        and facts.certificate_generation == 0
    )
    if not original_certified and not certified_after_return:
        return ManagedTerminalEligibility(
            evidence, False, "unknown", "FINAL_CERTIFICATE_MISMATCH", identity
        )
    original = bound.terminal_projection(identity)
    published = session._publisher.publish()
    if (
        published.valid is not True
        or published.verdict != verdict
        or published.output != original.output
        or published.receipt.original_projection_digest != identity.projection_digest
        or published.receipt.revision != identity.projection_revision + 1
    ):
        raise PermissionError("exact current certificate publication required")
    require_host_pending_turn(bound, expected_turn, identity)
    if session.final_evidence(expected_turn) != evidence:
        raise PermissionError("current outward final evidence changed")
    return ManagedTerminalEligibility(
        evidence,
        True,
        "certified" if original_certified else "certified_after_return",
        "CERTIFIED" if original_certified else "RECOVERED_CERTIFIED",
        identity,
        None,
        published,
    )
