"""Receipt-bound promotion of an explicit host fact claim.

The canonical verifier-subject path (``VerifiedSubjectFactPromotion``) only
ever writes the opaque ``verified-subject:<digest>`` token.  This service is
the second half of that authority chain: it writes semantic fact text, and it
accepts that text only when every persisted observation was produced by
``ReceiptObservationProducer`` from a host receipt whose ``claim_digest`` binds
exactly that text.  The claim originates in ``HostFinalFacts`` (populated by a
host verifier, never by model output), is sealed into the managed verifier
authority, and is persisted with the producer's insert capability, so callers
cannot supply worker, source, trust, or claim labels of their own.

The same hardening as the subject path applies:

* the fact identity is reserved and derived from project and claim, so a
  caller cannot overwrite an unrelated fact;
* the caller must hold the observation snapshot transaction
  (``BEGIN IMMEDIATE``) so a contradiction persisted concurrently cannot be
  omitted;
* any persisted authenticated verified negative for the claim demotes before
  any completeness check, so volume can never shield a contradicted fact;
* positive promotion reads the complete, bounded, project- and claim-scoped
  snapshot and needs the ladder's independent trusted evidence (distinct
  authenticated principals).

Ownership: the service holds only its ``LearningLadder``; the repository,
fact source, and connection are borrowed for one ``apply`` call and are used
on the caller's thread.  Instances are immutable after construction and safe
to share between threads as long as each call uses its own connection.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import sqlite3
from typing import Protocol

from .learning_ladder import LearningDecision, LearningLadder, LearningObservation
from .receipt_observation import ReceiptObservationProducer, VerifierReceipt


_MAX_OBSERVATIONS = 16
_MAX_SNAPSHOT = 10_000
_MAX_NEGATIVES = 16
# Bound shared with ``LearningObservation.content``: a longer claim could never
# be persisted as an observation, so it is refused at the request too.
_CLAIM_LIMIT = 512
_SUBJECT_PREFIX = "verified-subject:"
FACT_ID_PREFIX = "verified-claim-fact-"

ObservationPair = tuple[VerifierReceipt, LearningObservation]


class ClaimObservationRepository(Protocol):
    """The persisted, producer-authorized verifier observation snapshot."""

    def get(self, observation_id: str) -> ObservationPair | None: ...

    def list_subject_pairs(
        self, project_scope: str, content: str, *, limit: int = ...,
    ) -> tuple[ObservationPair, ...]: ...

    def list_subject_negative_pairs(
        self, project_scope: str, content: str, *, limit: int = ...,
    ) -> tuple[ObservationPair, ...]: ...


class AuthoritativeFactWriter(Protocol):
    """The scoped authoritative fact source and its atomic journal boundary."""

    def upsert_fact(
        self, connection: sqlite3.Connection, fact_id: str, project: str, text: str,
    ) -> object: ...

    def delete_fact(
        self, connection: sqlite3.Connection, fact_id: str, project: str,
    ) -> bool: ...


def claim_digest(content: str) -> str:
    """SHA-256 of the exact UTF-8 claim text carried by a host receipt."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _bounded_text(name: str, value: object, limit: int) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{name} must be bounded non-empty text")


@dataclass(frozen=True, slots=True)
class FactPromotionRequest:
    """One explicit claim transition named by persisted observation IDs.

    ``content`` is the claim the caller expects; it is accepted only if it is
    byte-identical to the persisted observation text and its receipt digest.
    """

    project: str
    fact_id: str
    content: str
    observation_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _bounded_text("project", self.project, 4096)
        _bounded_text("fact_id", self.fact_id, 4096)
        _bounded_text("content", self.content, _CLAIM_LIMIT)
        if self.content.startswith(_SUBJECT_PREFIX):
            raise ValueError("generic verifier subject tokens cannot become facts")
        if type(self.observation_ids) is not tuple or not (
            1 <= len(self.observation_ids) <= _MAX_OBSERVATIONS
        ):
            raise ValueError("one to sixteen observation identities are required")
        for item in self.observation_ids:
            _bounded_text("observation identity", item, 512)
        if len(set(self.observation_ids)) != len(self.observation_ids):
            raise ValueError("observation identities must be unique")


@dataclass(frozen=True, slots=True)
class FactPromotionResult:
    status: str  # promoted | candidate | demoted | unchanged
    decision: LearningDecision


class ReceiptBoundFactPromotion:
    """Apply one explicit, receipt-authenticated fact state transition."""

    def __init__(self, *, ladder: LearningLadder | None = None) -> None:
        self._ladder = ladder or LearningLadder()

    @staticmethod
    def fact_id_for_claim(project: str, content: str) -> str:
        """Reserved fact identity for one project's explicit host claim."""
        _bounded_text("project", project, 4096)
        _bounded_text("content", content, _CLAIM_LIMIT)
        if content.startswith(_SUBJECT_PREFIX):
            raise PermissionError("generic verifier subject tokens cannot become facts")
        scope = json.dumps(
            {"claim_digest": claim_digest(content), "project": project},
            sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        )
        return FACT_ID_PREFIX + hashlib.sha256(scope.encode("utf-8")).hexdigest()

    @staticmethod
    def _authenticated(pair: object, request: FactPromotionRequest, digest: str) -> bool:
        if type(pair) is not tuple or len(pair) != 2:
            return False
        receipt, observation = pair
        return (
            type(receipt) is VerifierReceipt
            and type(observation) is LearningObservation
            and receipt.project_scope == request.project
            and receipt.workspace_scope == request.project
            and receipt.verifier_outcome in {"passed", "failed"}
            and receipt.claim_digest == digest
            and observation.source == ReceiptObservationProducer.SOURCE
            and observation.trusted_source is True
            and observation.content == request.content
            and ("claim_digest:" + digest) in observation.provenance
        )

    def apply(
        self,
        request: FactPromotionRequest,
        *,
        repository: ClaimObservationRepository,
        fact_source: AuthoritativeFactWriter,
        connection: sqlite3.Connection,
    ) -> FactPromotionResult:
        """Promote or demote one claim fact from persisted receipt observations.

        A positive transition needs the ladder's independent trusted evidence
        over the complete claim snapshot.  Any authenticated negative receipt
        for the claim wins and tombstones the existing fact.  Both mutations
        use the source's project scope and atomic journal boundary.
        """
        if type(request) is not FactPromotionRequest:
            raise TypeError("typed fact promotion request is required")
        for name in ("get", "list_subject_pairs", "list_subject_negative_pairs"):
            if not callable(getattr(repository, name, None)):
                raise TypeError("claim-scoped verifier observation repository is required")
        if not callable(getattr(fact_source, "upsert_fact", None)) or not callable(
            getattr(fact_source, "delete_fact", None)
        ):
            raise TypeError("authoritative fact source is required")
        if not bool(getattr(connection, "in_transaction", False)):
            raise PermissionError("promotion requires an active observation snapshot transaction")
        digest = claim_digest(request.content)

        selected = tuple(repository.get(item) for item in request.observation_ids)
        for pair in selected:
            if pair is None:
                raise PermissionError("authenticated observation is unavailable")
            if type(pair) is not tuple or len(pair) != 2:
                raise PermissionError("repository returned an invalid authenticated observation")
            receipt, observation = pair
            if type(receipt) is not VerifierReceipt or type(observation) is not LearningObservation:
                raise PermissionError("repository returned an invalid authenticated observation")
            if receipt.project_scope != request.project or receipt.workspace_scope != request.project:
                raise PermissionError("observation scope does not match the fact project")
            if receipt.verifier_outcome not in {"passed", "failed"}:
                raise PermissionError("uncertain verifier observations cannot affect facts")
            if observation.source != ReceiptObservationProducer.SOURCE or observation.trusted_source is not True:
                raise PermissionError("only authenticated trusted observations may affect facts")
            if observation.content != request.content:
                raise PermissionError("fact content is not bound to the persisted observation claim")
            if receipt.claim_digest != digest or ("claim_digest:" + digest) not in observation.provenance:
                raise PermissionError("receipt does not carry an explicit host fact claim")

        if request.fact_id != self.fact_id_for_claim(request.project, request.content):
            raise PermissionError("fact identity is reserved for the verified claim")
        existing = connection.execute(
            "SELECT project, text FROM facts WHERE id=?", (request.fact_id,),
        ).fetchone()
        if existing is not None and (existing[0] != request.project or existing[1] != request.content):
            raise PermissionError("reserved verified claim fact identity is occupied")

        # A persisted authenticated negative demotes before any completeness
        # or snapshot check, so volume can never shield a contradicted fact.
        negatives = tuple(
            pair for pair in repository.list_subject_negative_pairs(
                request.project, request.content, limit=_MAX_NEGATIVES,
            )
            if self._authenticated(pair, request, digest)
            and pair[0].verifier_outcome == "failed"
            and pair[1].positive is False
        )
        if negatives:
            evaluated = {
                pair[1].observation_id: pair[1]
                for pair in (*(item for item in selected if item is not None), *negatives)
            }
            decision = self._ladder.evaluate(tuple(evaluated.values()))[0]
            deleted = fact_source.delete_fact(connection, request.fact_id, request.project)
            return FactPromotionResult("demoted" if deleted else "unchanged", decision)

        # Indexed read of this project's rows for this exact claim text only.
        claim_pairs = repository.list_subject_pairs(
            request.project, request.content, limit=_MAX_SNAPSHOT + 1,
        )
        if len(claim_pairs) > _MAX_SNAPSHOT:
            raise PermissionError("verifier observation snapshot is incomplete")
        if not claim_pairs:
            raise PermissionError("authenticated claim observations are unavailable")
        persisted_ids = {pair[1].observation_id for pair in claim_pairs}
        if not set(request.observation_ids).issubset(persisted_ids):
            raise PermissionError("observation snapshot changed")
        if not all(self._authenticated(pair, request, digest) for pair in claim_pairs):
            raise PermissionError("only scoped authenticated claim observations may promote")
        observations = tuple(pair[1] for pair in claim_pairs)
        decisions = self._ladder.evaluate(observations)
        if len(decisions) != 1:
            raise PermissionError("observations must describe one fact claim")
        decision = decisions[0]
        if any(not observation.positive for observation in observations):
            deleted = fact_source.delete_fact(connection, request.fact_id, request.project)
            return FactPromotionResult("demoted" if deleted else "unchanged", decision)
        if not decision.promotable:
            return FactPromotionResult("candidate", decision)
        fact_source.upsert_fact(connection, request.fact_id, request.project, request.content)
        return FactPromotionResult("promoted", decision)


__all__ = [
    "AuthoritativeFactWriter",
    "ClaimObservationRepository",
    "FACT_ID_PREFIX",
    "FactPromotionRequest",
    "FactPromotionResult",
    "ReceiptBoundFactPromotion",
    "claim_digest",
]
