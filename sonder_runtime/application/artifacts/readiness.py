"""Immutable readiness evidence for bounded fanout/fanin joins.

The barrier is deliberately storage neutral.  A producer creates one manifest
after its output is complete; the fanin owner validates all manifests before
making their content visible to aggregation.  This keeps a late, partial, or
cross-run artifact from becoming evidence by accident.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import re
from typing import Callable, Iterable, Mapping


READINESS_SCHEMA_VERSION = "sonder.artifact-readiness.v1"
COMPLETED = "completed"
# A fanout has no provider-independent wall-clock deadline, so the default is
# a conservative bounded ceiling rather than a short per-worker timeout. A
# production caller may pass a smaller run-specific window.
DEFAULT_MAX_AGE = timedelta(hours=24)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _digest(content: str | bytes) -> str:
    payload = content.encode("utf-8") if isinstance(content, str) else content
    if not isinstance(payload, bytes):
        raise TypeError("content must be text or bytes")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class ArtifactReadiness:
    """Bounded, immutable evidence that one producer completed one artifact."""

    producer_id: str
    run_id: str
    schema_version: str
    completion_marker: str
    content_digest: str
    validation_result: str
    timestamp: datetime
    deterministic_verifier: str | None = None
    artifact_id: str = ""
    size_bytes: int = -1
    # SHA-256 of the authoritative delegated source task; repo-target bytes
    # are checked separately by the fleet provenance verifier.
    source_revision: str = ""
    # Digest of the runtime's successful verifier metrics, not a signature.
    verifier_receipt: str = ""

    @classmethod
    def from_content(
        cls,
        producer_id: str,
        run_id: str,
        content: str | bytes,
        *,
        validation_result: str = "passed",
        timestamp: datetime | None = None,
        deterministic_verifier: str | None = None,
        source_revision: str = "",
        verifier_receipt: str = "",
    ) -> "ArtifactReadiness":
        raw = content.encode("utf-8") if isinstance(content, str) else content
        if not isinstance(raw, bytes):
            raise TypeError("content must be text or bytes")
        return cls(
            producer_id=producer_id,
            run_id=run_id,
            schema_version=READINESS_SCHEMA_VERSION,
            completion_marker=COMPLETED,
            content_digest=_digest(raw),
            validation_result=validation_result,
            timestamp=timestamp or datetime.now(timezone.utc),
            deterministic_verifier=deterministic_verifier,
            artifact_id=f"{run_id}/{producer_id}",
            size_bytes=len(raw),
            source_revision=source_revision,
            verifier_receipt=verifier_receipt,
        )

    def validate(
        self,
        *,
        expected_run_id: str,
        content: str | bytes | None = None,
        now: datetime | None = None,
        max_age: timedelta = DEFAULT_MAX_AGE,
        verifier: Callable[[str], bool] | None = None,
        expected_source_revision: str | None = None,
        expected_verifier_receipt: str | None = None,
        require_verifier_receipt: bool = False,
    ) -> None:
        """Reject incomplete, stale, malformed, or cross-run evidence."""
        if not self.producer_id or not self.run_id or self.run_id != expected_run_id:
            raise ValueError("artifact readiness producer/run identity mismatch")
        if self.schema_version != READINESS_SCHEMA_VERSION:
            raise ValueError("unsupported artifact readiness schema")
        if self.completion_marker != COMPLETED:
            raise ValueError("artifact readiness completion marker is incomplete")
        if self.validation_result != "passed":
            raise ValueError("artifact validation did not pass")
        if self.artifact_id and self.artifact_id != f"{expected_run_id}/{self.producer_id}":
            raise ValueError("artifact readiness artifact identity mismatch")
        if type(self.size_bytes) is not int or self.size_bytes < -1:
            raise ValueError("artifact readiness size is invalid")
        if expected_source_revision is not None:
            if (not isinstance(expected_source_revision, str)
                or not _SHA256.fullmatch(expected_source_revision)
                or self.source_revision != expected_source_revision):
                raise ValueError("artifact readiness source revision mismatch")
            if not self.artifact_id or self.size_bytes < 0 or content is None:
                raise ValueError("artifact readiness source binding needs artifact bytes and identity")
        if not isinstance(self.source_revision, str) or (
            self.source_revision and not _SHA256.fullmatch(self.source_revision)
        ):
            raise ValueError("artifact readiness source revision is invalid")
        if not isinstance(self.verifier_receipt, str) or (
            self.verifier_receipt and not _SHA256.fullmatch(self.verifier_receipt)
        ):
            raise ValueError("artifact readiness verifier receipt is invalid")
        if expected_verifier_receipt is not None and (
                not isinstance(expected_verifier_receipt, str)
                or not _SHA256.fullmatch(expected_verifier_receipt)
                or self.verifier_receipt != expected_verifier_receipt):
            raise ValueError("artifact readiness verifier receipt mismatch")
        if require_verifier_receipt and not self.verifier_receipt:
            raise ValueError("artifact readiness verifier receipt is missing")
        if not isinstance(self.timestamp, datetime) or self.timestamp.tzinfo is None:
            raise ValueError("artifact readiness timestamp must be timezone-aware")
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None or self.timestamp > current:
            raise ValueError("artifact readiness timestamp is in the future")
        if current - self.timestamp > max_age:
            raise ValueError("stale artifact readiness evidence")
        if not isinstance(self.content_digest, str) or len(self.content_digest) != 64 or any(
            char not in "0123456789abcdef" for char in self.content_digest.lower()
        ):
            raise ValueError("artifact readiness content digest is invalid")
        if content is not None:
            raw = content.encode("utf-8") if isinstance(content, str) else content
            if not isinstance(raw, bytes):
                raise TypeError("content must be text or bytes")
            if _digest(raw) != self.content_digest:
                raise ValueError("artifact readiness content digest mismatch")
            if self.size_bytes >= 0 and len(raw) != self.size_bytes:
                raise ValueError("artifact readiness content size mismatch")
        if verifier is not None:
            if not self.deterministic_verifier:
                raise ValueError("deterministic verifier identity is missing")
            verifier_content = content.decode("utf-8") if isinstance(content, bytes) else str(content or "")
            if not verifier(verifier_content):
                raise ValueError("deterministic artifact verifier rejected output")


class ArtifactReadinessBarrier:
    """Validate the complete bounded producer set at a fanin boundary."""

    def __init__(self, *, max_age: timedelta = DEFAULT_MAX_AGE) -> None:
        if max_age <= timedelta(0):
            raise ValueError("max_age must be positive")
        self.max_age = max_age

    def join(
        self,
        artifacts: Iterable[ArtifactReadiness],
        *,
        run_id: str,
        expected_producers: Iterable[str],
        content_by_producer: Mapping[str, str | bytes] | None = None,
        now: datetime | None = None,
        verifier: Callable[[str], bool] | None = None,
        expected_source_revisions: Mapping[str, str] | None = None,
        expected_verifier_receipts: Mapping[str, str] | None = None,
        require_verifier_receipt: bool = False,
    ) -> tuple[ArtifactReadiness, ...]:
        expected = tuple(str(item) for item in expected_producers)
        if not expected or len(expected) != len(set(expected)):
            raise ValueError("fanout producer set must be non-empty and unique")
        found = tuple(artifacts)
        if len(found) != len(expected):
            raise ValueError("fanout artifact set is partial")
        if content_by_producer is not None and set(content_by_producer) != set(expected):
            raise ValueError("fanout content map is incomplete")
        if expected_source_revisions is not None and set(expected_source_revisions) != set(expected):
            raise ValueError("fanout source revision map is incomplete")
        if expected_verifier_receipts is not None and set(expected_verifier_receipts) != set(expected):
            raise ValueError("fanout verifier receipt map is incomplete")
        if require_verifier_receipt and expected_verifier_receipts is None:
            raise ValueError("fanout verifier receipts require independent expected values")
        by_producer = {item.producer_id: item for item in found}
        if len(by_producer) != len(found) or set(by_producer) != set(expected):
            raise ValueError("fanout artifact producer set does not match run")
        for producer in expected:
            item = by_producer[producer]
            item.validate(
                expected_run_id=run_id,
                content=(content_by_producer or {}).get(producer),
                now=now,
                max_age=self.max_age,
                verifier=verifier,
                expected_source_revision=expected_source_revisions[producer]
                if expected_source_revisions is not None else None,
                expected_verifier_receipt=expected_verifier_receipts[producer]
                if expected_verifier_receipts is not None else None,
                require_verifier_receipt=require_verifier_receipt,
            )
        return tuple(by_producer[producer] for producer in expected)


__all__ = ["ArtifactReadiness", "ArtifactReadinessBarrier", "READINESS_SCHEMA_VERSION"]
