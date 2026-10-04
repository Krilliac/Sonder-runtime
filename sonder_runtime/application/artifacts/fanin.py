"""Small fan-in adapters: validate producer evidence without changing good bytes.

Owners enumerate their expected slots and retain an explicit rejection for each
missing/invalid slot. Evidence is created only by the producer, never here.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta
import json

from .readiness import DEFAULT_MAX_AGE, ArtifactReadiness, ArtifactReadinessBarrier


def encode_readiness(evidence: ArtifactReadiness) -> str:
    data = asdict(evidence)
    data["timestamp"] = evidence.timestamp.isoformat()
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def decode_readiness(value: str) -> ArtifactReadiness | None:
    try:
        data = json.loads(value)
        data["timestamp"] = datetime.fromisoformat(data["timestamp"])
        return ArtifactReadiness(**data)
    except (TypeError, ValueError, KeyError, AttributeError):
        return None


def readiness_error(
    readiness: ArtifactReadiness | None,
    *,
    run_id: str,
    producer_id: str,
    content: str | bytes,
    source_revision: str | None = None,
    verifier_receipt: str | None = None,
    require_verifier_receipt: bool = False,
    max_age: timedelta = DEFAULT_MAX_AGE,
) -> str:
    """Return a content-free rejection, or empty text for a validated slot.

    A configured verifier requires an independently supplied expected receipt;
    no verifier is re-executed on the aggregation path.
    """
    if not isinstance(readiness, ArtifactReadiness):
        return "artifact readiness evidence is missing or malformed"
    try:
        ArtifactReadinessBarrier(max_age=max_age).join(
            (readiness,), run_id=run_id, expected_producers=(producer_id,),
            content_by_producer={producer_id: content},
            expected_source_revisions=(
                {producer_id: source_revision} if source_revision is not None else None
            ),
            expected_verifier_receipts=(
                {producer_id: verifier_receipt} if verifier_receipt is not None else None
            ),
            require_verifier_receipt=require_verifier_receipt,
        )
    except (TypeError, ValueError, AttributeError, OverflowError):
        # Do not echo arbitrary producer fields or exception content into a
        # downstream prompt. The detailed validator remains available locally.
        return "artifact readiness validation failed (identity, digest, completion, age, or verifier receipt)"
    return ""
