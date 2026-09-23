"""Private current terminal decision; returned values confer no authority."""

from dataclasses import asdict

from ..application.ports.terminal_eligibility import ManagedTerminalEligibility
from ..application.ports.terminal_eligibility import _issue_host_verifier_authority
from ..application.ports.lane_continuation import (
    PendingApprovalEvidence,
    PendingVerificationIdentity,
)
from ..application.agents.host_turns import require_host_pending_turn
from ..application.ports.delegated_verification import digest
from .standalone_continuation import PublishedHostTerminal


def _with_authority(
    value, session, expected_turn, verifier_factory,
):
    """Attach a live resolver only to decisions produced by this boundary."""
    return ManagedTerminalEligibility(
        value.evidence, value.eligible, value.phase, value.code,
        value.pending_identity, value.pending_approval, value.published,
        value.authenticated_worker_id, value.verified_subject_digest,
        value.verified_failure_receipt,
        _issue_host_verifier_authority(
            lambda: terminal_eligibility(
                session, expected_turn, verifier_factory=verifier_factory
            )
        ),
    )


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
    if phase == "failed" and code == "VERIFICATION_CHECK_FAILED":
        failure = view.get("failure_receipt")
        if not isinstance(failure, dict):
            return ManagedTerminalEligibility(
                evidence, False, "unknown", "FAILURE_RECEIPT_UNAVAILABLE", identity
            )
        supplied_digest = failure.get("receipt_digest")
        unsigned = dict(failure)
        unsigned.pop("receipt_digest", None)
        try:
            prepared = session._bound.prepared_verification(identity)
            if (
                failure.get("schema") != "delegated-verification-failure-v1"
                or not isinstance(supplied_digest, str)
                or supplied_digest != digest(unsigned)
                or failure.get("verification_id") != identity.verification_id
                or failure.get("generation") != identity.generation
                or failure.get("bundle") != prepared.approval_payload()
                or failure.get("before_manifest_digest") != failure.get("after_manifest_digest")
                or not isinstance(failure.get("before_manifest_digest"), str)
                or len(failure["before_manifest_digest"]) != 64
                or any(
                    char not in "0123456789abcdef"
                    for char in failure["before_manifest_digest"]
                )
            ):
                raise ValueError("failure receipt binding changed")
            index = failure.get("failed_check_index")
            if type(index) is not int or not 0 <= index < len(prepared.checks):
                raise ValueError("failure check identity unavailable")
            expected_check = asdict(prepared.checks[index])
            expected_check["argv"] = list(expected_check["argv"])
            if failure.get("failed_check") != expected_check:
                raise ValueError("failure check identity changed")
            job_id = failure.get("failed_job_id")
            proofs = failure.get("cleanup_proofs")
            failed_proof = failure.get("failed_proof")
            if (
                not isinstance(job_id, str)
                or not isinstance(proofs, list)
                or len(proofs) != len(view.get("job_ids", ()))
                or not isinstance(failed_proof, dict)
                or failed_proof.get("job_id") != job_id
                or failed_proof.get("status") != "failed"
                or type(failed_proof.get("exit_code")) is not int
                or failed_proof.get("exit_code") == 0
            ):
                raise ValueError("specific failed check proof unavailable")
            proof_by_job = {
                item.get("job_id"): item for item in proofs if isinstance(item, dict)
            }
            if len(proof_by_job) != len(view.get("job_ids", ())):
                raise ValueError("cleanup proof identities changed")
            if proof_by_job.get(job_id) != failed_proof:
                raise ValueError("failed check proof changed")
            for expected_job in view.get("job_ids", ()):
                proof = proof_by_job.get(expected_job)
                if proof is None or session._verifier._proof(
                    expected_job, identity.parent_session_id, session.context.principal_id
                ) != proof:
                    raise ValueError("cleanup proof changed")
            current = session._verifier.snapshotter.capture(tuple(prepared.roots))
            if current.digest != failure["after_manifest_digest"]:
                raise ValueError("failure source manifest changed")
            session._verifier._require_current(
                prepared, session.context, exact_context=False
            )
            if facts.project_scope not in prepared.roots:
                raise ValueError("failure scope is outside prepared roots")
            verified_subject_digest = digest({
                "project_scope": facts.project_scope,
                "source_manifest": failure["before_manifest_digest"],
                "checks": tuple(
                    (check.target, check.catalog_digest, check.argv_digest, check.workspace_root)
                    for check in prepared.checks
                ),
            })
        except (ValueError, KeyError, OSError, PermissionError):
            return ManagedTerminalEligibility(
                evidence, False, "unknown", "FAILURE_RECEIPT_INVALID", identity
            )
        if len(prepared.children) != 1 or not prepared.children[0][0]:
            return ManagedTerminalEligibility(
                evidence, False, "unknown", "WORKER_ATTRIBUTION_AMBIGUOUS", identity
            )
        return _with_authority(ManagedTerminalEligibility(
            evidence,
            False,
            "failed",
            "CHECK_FAILED",
            identity,
            None,
            None,
            prepared.children[0][0],
            verified_subject_digest,
            failure,
        ), session, expected_turn, verifier_factory)
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
    if len(prepared.children) != 1 or not prepared.children[0][0]:
        return ManagedTerminalEligibility(
            evidence, False, "unknown", "WORKER_ATTRIBUTION_AMBIGUOUS", identity
        )
    authenticated_worker_id = prepared.children[0][0]
    certificate = view.get("certificate")
    manifest_digest = (
        certificate.get("before_manifest_digest")
        if isinstance(certificate, dict) else None
    )
    if (
        not isinstance(manifest_digest, str)
        or len(manifest_digest) != 64
        or any(char not in "0123456789abcdef" for char in manifest_digest)
        or certificate.get("after_manifest_digest") != manifest_digest
    ):
        return ManagedTerminalEligibility(
            evidence, False, "unknown", "CERTIFICATE_SUBJECT_UNAVAILABLE", identity
        )
    verified_subject_digest = digest({
        "project_scope": facts.project_scope,
        "source_manifest": manifest_digest,
        "checks": tuple(
            (check.target, check.catalog_digest, check.argv_digest, check.workspace_root)
            for check in prepared.checks
        ),
    })
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
        or digest(certificate) != published.receipt.certificate_digest
        or published.receipt.revision != identity.projection_revision + 1
    ):
        raise PermissionError("exact current certificate publication required")
    require_host_pending_turn(bound, expected_turn, identity)
    if session.final_evidence(expected_turn) != evidence:
        raise PermissionError("current outward final evidence changed")
    return _with_authority(ManagedTerminalEligibility(
        evidence,
        True,
        "certified" if original_certified else "certified_after_return",
        "CERTIFIED" if original_certified else "RECOVERED_CERTIFIED",
        identity,
        None,
        published,
        authenticated_worker_id,
        verified_subject_digest,
    ), session, expected_turn, verifier_factory)
