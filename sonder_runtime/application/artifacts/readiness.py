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
from typing import Callable, Iterable, Mapping


READINESS_SCHEMA_VERSION = "sonder.artifact-readiness.v1"
COMPLETED = "completed"
# A fanout has no provider-independent wall-clock deadline, so the default is
# a conservative bounded ceiling rather than a short per-worker timeout. A
# production caller may pass a smaller run-specific window.
DEFAULT_MAX_AGE = timedelta(hours=24)


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
    ) -> "ArtifactReadiness":
        return cls(
            producer_id=producer_id,
            run_id=run_id,
            schema_version=READINESS_SCHEMA_VERSION,
            completion_marker=COMPLETED,
            content_digest=_digest(content),
            validation_result=validation_result,
            timestamp=timestamp or datetime.now(timezone.utc),
            deterministic_verifier=deterministic_verifier,
        )

    def validate(
        self,
        *,
        expected_run_id: str,
        content: str | bytes | None = None,
        now: datetime | None = None,
        max_age: timedelta = DEFAULT_MAX_AGE,
        verifier: Callable[[str], bool] | None = None,
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
        if not isinstance(self.timestamp, datetime) or self.timestamp.tzinfo is None:
            raise ValueError("artifact readiness timestamp must be timezone-aware")
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None or self.timestamp > current:
            raise ValueError("artifact readiness timestamp is in the future")
        if current - self.timestamp > max_age:
            raise ValueError("stale artifact readiness evidence")
        if len(self.content_digest) != 64 or any(
            char not in "0123456789abcdef" for char in self.content_digest.lower()
        ):
            raise ValueError("artifact readiness content digest is invalid")
        if content is not None and _digest(content) != self.content_digest:
            raise ValueError("artifact readiness content digest mismatch")
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
    ) -> tuple[ArtifactReadiness, ...]:
        expected = tuple(str(item) for item in expected_producers)
        if not expected or len(expected) != len(set(expected)):
            raise ValueError("fanout producer set must be non-empty and unique")
        found = tuple(artifacts)
        if len(found) != len(expected):
            raise ValueError("fanout artifact set is partial")
        if content_by_producer is not None and set(content_by_producer) != set(expected):
            raise ValueError("fanout content map is incomplete")
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
            )
        return tuple(by_producer[producer] for producer in expected)


__all__ = ["ArtifactReadiness", "ArtifactReadinessBarrier", "READINESS_SCHEMA_VERSION"]
