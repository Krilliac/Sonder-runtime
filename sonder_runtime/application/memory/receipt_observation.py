"""Host-authenticated verifier receipts as durable learning observations.

The producer accepts only the typed host-final evidence returned by the real
managed verification boundary.  It deliberately has no ``source``, worker, or
independence arguments: those values come from the authenticated receipt and
its bound authority scope.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json

from ..ports.host_turn_links import ManagedHostFinalEvidence
from .learning_ladder import LearningObservation


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


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
    receipt_digest: str
    authority_scope: str

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
        for name in ("content_digest", "receipt_digest"):
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

    @classmethod
    def from_host_final(cls, evidence: ManagedHostFinalEvidence) -> tuple[VerifierReceipt, LearningObservation]:
        if type(evidence) is not ManagedHostFinalEvidence:
            raise TypeError("typed host-final evidence is required")
        link = evidence.result.receipt
        facts = evidence.facts
        turn = link.turn
        if facts.delegated_work is not True:
            raise ValueError("verifier observation requires delegated host work")
        if not facts.certificate_id or facts.certificate_generation < 1:
            raise ValueError("authenticated verifier certificate is required")
        if not facts.certificate_code:
            raise ValueError("verifier certificate code is required")
        if facts.validation_passed and facts.terminal_class == "NORMAL":
            outcome = "passed"
        elif facts.validation_attempted and facts.terminal_class == "VALIDATION_FAILED":
            outcome = "failed"
        else:
            outcome = "uncertain"
        authority_scope = _digest({
            "principal_id": turn.principal_id,
            "workspace_scope": facts.project_scope,
        })
        receipt = VerifierReceipt(
            receipt_id=link.receipt_digest,
            interaction_id=turn.host_conversation_id,
            run_id=turn.run_id,
            principal_id=turn.principal_id,
            project_scope=facts.project_scope,
            workspace_scope=facts.project_scope,
            verifier_outcome=outcome,
            content_digest=link.output_digest,
            receipt_digest=link.receipt_digest,
            authority_scope=authority_scope,
        )
        # The certificate code is host-authenticated and receipt-bound.  Model
        # output is never accepted as the claim text or as a trust label.
        observation = LearningObservation(
            observation_id="observation-" + receipt.receipt_id,
            content=facts.certificate_code,
            source=cls.SOURCE,
            independent_key=_digest({
                "principal_id": receipt.principal_id,
                "authority_scope": receipt.authority_scope,
            }),
            provenance=(
                "receipt:" + receipt.receipt_id,
                "receipt_digest:" + receipt.receipt_digest,
                "content_digest:" + receipt.content_digest,
                "run:" + receipt.run_id,
            ),
            confidence=1.0 if outcome in {"passed", "failed"} else 0.0,
            positive=outcome == "passed",
            evaluation_passed=outcome == "passed",
            trusted_source=outcome in {"passed", "failed"},
        )
        return receipt, observation


__all__ = ["ReceiptObservationProducer", "VerifierReceipt"]
