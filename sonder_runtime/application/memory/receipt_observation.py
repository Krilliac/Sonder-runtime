"""Host-authenticated verifier receipts as durable learning observations.

The producer accepts only the typed current eligibility decision returned by
the real managed verification boundary. It deliberately has no ``source``,
worker, or independence arguments: those values come from the authenticated
certificate and its bound authority scope.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import json

from ..ports.terminal_eligibility import (
    ManagedTerminalEligibility,
    _HostVerifierAuthority,
)
from .learning_ladder import LearningObservation


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


_OBSERVATION_AUTH_SEAL = object()


def _receipt_payload(receipt) -> dict:
    """Return the immutable receipt fields, excluding its live capability."""
    return {
        name: getattr(receipt, name)
        for name in receipt.__dataclass_fields__
        if name != "authorization"
    }


def _observation_payload(observation) -> dict:
    value = {
        name: getattr(observation, name)
        for name in observation.__dataclass_fields__
    }
    value["observed_at"] = observation.observed_at.isoformat()
    value["provenance"] = list(observation.provenance)
    return value


def _authorization_digest(receipt, observation) -> str:
    return _digest({
        "receipt": _receipt_payload(receipt),
        "observation": _observation_payload(observation),
    })


class _ObservationAuthorization:
    __slots__ = ("_binding_digest",)

    def __init__(self, binding_digest, seal):
        if seal is not _OBSERVATION_AUTH_SEAL:
            raise TypeError("observation authorization is private")
        self._binding_digest = binding_digest

    def matches(self, receipt, observation) -> bool:
        return (
            type(receipt) is VerifierReceipt
            and type(observation) is LearningObservation
            and self._binding_digest == _authorization_digest(receipt, observation)
        )


def _issue_observation_authorization(receipt, observation):
    return _ObservationAuthorization(
        _authorization_digest(receipt, observation),
        _OBSERVATION_AUTH_SEAL,
    )


@dataclass(frozen=True, slots=True)
class VerifierReceipt:
    """Content-free identity and outcome facts retained from one host receipt."""

    receipt_id: str
    interaction_id: str
    run_id: str
    principal_id: str
    project_scope: str
    workspace_scope: str
    verifier_outcome: str
    content_digest: str
    subject_digest: str
    receipt_digest: str
    authority_scope: str
    authorization: object | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        for name in (
            "receipt_id", "interaction_id", "run_id", "principal_id",
            "project_scope", "workspace_scope", "authority_scope",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or len(value) > 4096:
                raise ValueError(f"{name} must be bounded non-empty text")
        if self.verifier_outcome not in {"passed", "failed", "uncertain"}:
            raise ValueError("unsupported verifier outcome")
        for name in ("content_digest", "subject_digest", "receipt_digest"):
            value = getattr(self, name)
            if not isinstance(value, str) or len(value) != 64 or any(
                char not in "0123456789abcdef" for char in value
            ):
                raise ValueError(f"{name} must be a SHA-256 digest")
        if self.receipt_id != self.receipt_digest:
            raise ValueError("receipt identity must equal its receipt digest")


class ReceiptObservationProducer:
    """Build observations from host-owned, authenticated verifier evidence."""

    SOURCE = "authenticated_verifier"

    @staticmethod
    def _subject(eligibility: ManagedTerminalEligibility, facts) -> tuple[str, str]:
        subject_digest = eligibility.verified_subject_digest
        if (
            not isinstance(subject_digest, str) or len(subject_digest) != 64
            or any(char not in "0123456789abcdef" for char in subject_digest)
        ):
            raise PermissionError("verified subject identity is unavailable")
        return "verified-subject:" + subject_digest, subject_digest

    @classmethod
    def from_terminal_eligibility(cls, eligibility: ManagedTerminalEligibility) -> tuple[VerifierReceipt, LearningObservation]:
        if type(eligibility) is not ManagedTerminalEligibility:
            raise TypeError("verified terminal eligibility is required")
        authority = eligibility.authority
        resolver = getattr(authority, "resolve", None)
        if type(authority) is not _HostVerifierAuthority or not callable(resolver):
            raise PermissionError(
                "owner-bound managed verifier authority is required"
            )
        # Do not trust any fields on the public eligibility value.  The live
        # resolver re-reads the current owner-bound durable host turn and
        # verifier result, so a copied or modified dataclass cannot mint a
        # trusted observation.
        eligibility = resolver()
        if type(eligibility) is not ManagedTerminalEligibility:
            raise PermissionError("owner-bound managed verifier result is invalid")
        if eligibility.phase in {"certified", "certified_after_return"}:
            if eligibility.eligible is not True:
                raise PermissionError("current certified terminal eligibility is required")
        elif eligibility.phase == "failed":
            # A verified failed check is learning evidence, never permission
            # to publish a successful outward terminal response.
            if eligibility.eligible is not False:
                raise PermissionError("failed verification cannot complete the host turn")
        else:
            raise PermissionError("current certified terminal eligibility is required")
        if not eligibility.authenticated_worker_id:
            raise PermissionError("unambiguous authenticated worker attribution is required")
        evidence = eligibility.evidence
        link = evidence.result.receipt
        facts = evidence.facts
        turn = link.turn
        if facts.delegated_work is not True:
            raise ValueError("verifier observation requires delegated host work")
        failure = eligibility.verified_failure_receipt
        if eligibility.phase == "failed":
            if not isinstance(failure, dict) or failure.get("schema") != "delegated-verification-failure-v1":
                raise PermissionError("immutable verifier failure receipt is required")
            unsigned = dict(failure)
            supplied_digest = unsigned.pop("receipt_digest", None)
            manifest_digest = failure.get("before_manifest_digest")
            failed_proof = failure.get("failed_proof")
            if (
                not isinstance(supplied_digest, str)
                or supplied_digest != _digest(unsigned)
                or failure.get("after_manifest_digest") != manifest_digest
                or not isinstance(manifest_digest, str)
                or len(manifest_digest) != 64
                or any(char not in "0123456789abcdef" for char in manifest_digest)
                or not isinstance(failure.get("failed_check"), dict)
                or not isinstance(failed_proof, dict)
                or failed_proof.get("status") != "failed"
                or type(failed_proof.get("exit_code")) is not int
                or failed_proof.get("exit_code") == 0
            ):
                raise PermissionError("immutable verifier failure receipt is invalid")
            outcome = "failed"
        else:
            if not facts.certificate_id or facts.certificate_generation < 1:
                raise ValueError("authenticated verifier certificate is required")
            if not facts.certificate_code:
                raise ValueError("verifier certificate code is required")
            if facts.validation_passed is not True or facts.terminal_class != "NORMAL":
                raise PermissionError(
                    "independently verified negative evidence is not available at this boundary"
                )
            outcome = "passed"
        if eligibility.phase == "failed" and (
            failure.get("failed_check") is None
            or failure.get("failed_proof", {}).get("status") == "succeeded"
            or failure.get("failed_proof", {}).get("exit_code") == 0
        ):
            raise PermissionError(
                "specific failed check proof is required"
            )
        claim, subject_digest = cls._subject(eligibility, facts)
        authority_scope = _digest({
            "worker_id": eligibility.authenticated_worker_id,
            "workspace_scope": facts.project_scope,
        })
        receipt = VerifierReceipt(
            receipt_id=(failure["receipt_digest"] if outcome == "failed" else link.receipt_digest),
            interaction_id=turn.host_conversation_id,
            run_id=turn.run_id,
            principal_id=turn.principal_id,
            project_scope=facts.project_scope,
            workspace_scope=facts.project_scope,
            verifier_outcome=outcome,
            content_digest=link.output_digest,
            subject_digest=subject_digest,
            receipt_digest=(failure["receipt_digest"] if outcome == "failed" else link.receipt_digest),
            authority_scope=authority_scope,
        )
        # The subject digest is bound to the verified check bundle and scope.
        # Verdict tokens and model output are never accepted as claim text.
        observation = LearningObservation(
            observation_id="observation-" + receipt.receipt_id,
            content=claim,
            source=cls.SOURCE,
            independent_key=_digest({
                "worker_id": eligibility.authenticated_worker_id,
                "authority_scope": receipt.authority_scope,
            }),
            provenance=(
                "receipt:" + receipt.receipt_id,
                "receipt_digest:" + receipt.receipt_digest,
                "content_digest:" + receipt.content_digest,
                "subject:" + subject_digest,
                "run:" + receipt.run_id,
                *(() if outcome == "passed" else (
                    "failed_check:" + _digest(failure["failed_check"]),
                    "failure_manifest:" + failure["before_manifest_digest"],
                )),
            ),
            confidence=1.0 if outcome in {"passed", "failed"} else 0.0,
            positive=outcome == "passed",
            evaluation_passed=outcome == "passed",
            trusted_source=outcome in {"passed", "failed"},
        )
        return replace(
            receipt,
            authorization=_issue_observation_authorization(receipt, observation),
        ), observation


class VerifiedSubjectFactPromotion:
    """Promote only canonical verifier subjects through the live fact source.

    The request names observation IDs, never semantic fact text.  The fact
    content is derived from the persisted producer observation, and the
    authoritative source is supplied by the application composition root.
    """

    def __init__(self, *, ladder=None) -> None:
        from .learning_ladder import LearningLadder
        self._ladder = ladder or LearningLadder()

    @staticmethod
    def fact_id_for_subject(content: str) -> str:
        if (
            not isinstance(content, str)
            or not content.startswith("verified-subject:")
            or len(content) != len("verified-subject:") + 64
            or any(char not in "0123456789abcdef" for char in content.split(":", 1)[1])
        ):
            raise PermissionError("canonical verifier subject identity is required")
        return "verified-subject-fact-" + content.split(":", 1)[1]

    def apply(self, *, project, fact_id, observation_ids, repository, fact_source, connection):
        if not isinstance(project, str) or not project.strip():
            raise ValueError("fact project is required")
        if not isinstance(fact_id, str) or not fact_id.strip():
            raise ValueError("fact identity is required")
        if type(observation_ids) is not tuple or not 1 <= len(observation_ids) <= 16:
            raise ValueError("observation identities are required")
        if len(set(observation_ids)) != len(observation_ids):
            raise ValueError("observation identities must be unique")
        if not callable(getattr(repository, "get", None)):
            raise TypeError("verifier observation repository is required")
        if not callable(getattr(fact_source, "upsert_fact", None)) or not callable(
            getattr(fact_source, "delete_fact", None)
        ):
            raise TypeError("authoritative fact source is required")
        if not bool(getattr(connection, "in_transaction", False)):
            raise PermissionError("promotion requires an active observation snapshot transaction")
        selected = tuple(repository.get(item) for item in observation_ids)
        if any(pair is None for pair in selected):
            raise PermissionError("authenticated observation is unavailable")
        selected_observations = tuple(pair[1] for pair in selected)
        if len({observation.content_key for observation in selected_observations}) != 1:
            raise PermissionError("observations must describe one verified subject")
        content = selected_observations[0].content
        expected_fact_id = self.fact_id_for_subject(content)
        if fact_id != expected_fact_id:
            raise PermissionError("fact identity is reserved for the verified subject")
        existing = connection.execute(
            "SELECT project, text FROM facts WHERE id=?",
            (fact_id,),
        ).fetchone()
        if existing is not None and (existing[0] != project or existing[1] != content):
            raise PermissionError("reserved verifier subject fact identity is occupied")
        list_pairs = getattr(repository, "list_pairs", None)
        if not callable(list_pairs):
            raise TypeError("complete verifier observation snapshot is required")
        complete = list_pairs(limit=10_001)
        if len(complete) > 10_000:
            raise PermissionError("verifier observation snapshot is incomplete")
        subject_pairs = tuple(
            pair for pair in complete
            if pair[0].project_scope == project
            and pair[0].workspace_scope == project
            and pair[1].content_key == selected_observations[0].content_key
        )
        if not subject_pairs:
            raise PermissionError("authenticated subject observations are unavailable")
        persisted_ids = {pair[1].observation_id for pair in subject_pairs}
        if not set(observation_ids).issubset(persisted_ids):
            raise PermissionError("observation snapshot changed")
        pairs = subject_pairs
        observations = tuple(pair[1] for pair in pairs)
        if any(
            receipt.project_scope != project
            or receipt.workspace_scope != project
            or receipt.verifier_outcome not in {"passed", "failed"}
            or observation.source != ReceiptObservationProducer.SOURCE
            or observation.trusted_source is not True
            or not observation.content.startswith("verified-subject:")
            for receipt, observation in pairs
        ):
            raise PermissionError("only scoped authenticated verifier observations may promote")
        content_keys = {observation.content_key for observation in observations}
        if len(content_keys) != 1:
            raise PermissionError("observations must describe one verified subject")
        decisions = self._ladder.evaluate(observations)
        if len(decisions) != 1:
            raise PermissionError("observations must describe one verified subject")
        decision = decisions[0]
        if any(not observation.positive for observation in observations):
            mutation = fact_source.delete_fact(connection, fact_id, project)
            return "demoted" if mutation else "unchanged", decision
        if not decision.promotable:
            return "candidate", decision
        mutation = fact_source.upsert_fact(
            connection, fact_id, project, content,
        )
        return "promoted", decision


__all__ = ["ReceiptObservationProducer", "VerifierReceipt", "VerifiedSubjectFactPromotion"]
