"""Master-orchestrator fan-in validation.

This adapter keeps the root orchestration module focused on scheduling.  It
validates every expected producer independently so a malformed slot remains
visible to synthesis while valid sibling bytes retain their original order
and content.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone

from .fanin import readiness_error
from .readiness import ArtifactReadiness


def validate_master_slots_for_run(
    *,
    run_id: str,
    expected_producers: Sequence[str],
    readiness_by_producer: Mapping[str, ArtifactReadiness],
    content_by_producer: Mapping[str, str],
    source_revisions: Mapping[str, str],
    require_verifier_receipt: Mapping[str, bool],
    expected_verifier_receipts: Mapping[str, str | None],
    max_age: timedelta,
) -> dict[str, str]:
    """Run the bounded readiness helper against all expected producer IDs."""
    errors: dict[str, str] = {}
    for producer in expected_producers:
        artifact = readiness_by_producer.get(producer)
        if isinstance(artifact, ArtifactReadiness):
            timestamp = artifact.timestamp
            now = datetime.now(timezone.utc)
            if (
                not isinstance(timestamp, datetime)
                or timestamp.tzinfo is None
                or timestamp > now
                or now - timestamp > max_age
            ):
                errors[producer] = "artifact readiness validation failed (identity, digest, completion, age, or verifier receipt)"
                continue
        reason = readiness_error(
            artifact,
            run_id=run_id,
            producer_id=producer,
            content=content_by_producer.get(producer, ""),
            source_revision=source_revisions.get(producer),
            verifier_receipt=expected_verifier_receipts.get(producer),
            require_verifier_receipt=bool(require_verifier_receipt.get(producer)),
        )
        if reason:
            errors[producer] = reason
    return errors


__all__ = ["validate_master_slots_for_run"]
