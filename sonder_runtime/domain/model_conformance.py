"""Evidence gates for model backend and distributed deployment readiness.

The gate records what a backend actually verified.  It never probes a provider,
turns a capability advertisement into proof, or estimates throughput from
aggregate machine memory.  Runtime adapters can publish this value after their
offline/live conformance suites complete.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
import re


_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
SUPPORTED_OPERATIONS = (
    "cancellation",
    "generation",
    "health",
    "replacement",
    "shard_loss_recovery",
    "streaming",
    "transport_failure",
)
REQUIRED_OPERATIONS = (
    "cancellation",
    "generation",
    "health",
    "replacement",
    "streaming",
    "transport_failure",
)


class ConformanceError(ValueError):
    """Malformed or internally inconsistent backend evidence."""


def _identity(value: object, field: str) -> str:
    if not isinstance(value, str) or not _IDENTITY.fullmatch(value):
        raise ConformanceError(f"{field} must be a bounded stable identity")
    return value


def _digest(value: object, field: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ConformanceError(f"{field} must be a canonical SHA-256 digest")
    return value


def _operations(values: object, field: str) -> tuple[str, ...]:
    if type(values) is not tuple or not values or len(values) > len(SUPPORTED_OPERATIONS):
        raise ConformanceError(f"{field} must be a bounded tuple of operations")
    normalized = tuple(values)
    if any(value not in SUPPORTED_OPERATIONS for value in normalized):
        raise ConformanceError(f"{field} contains an unknown operation")
    if len(set(normalized)) != len(normalized):
        raise ConformanceError(f"{field} contains a duplicate operation")
    return tuple(sorted(normalized))


def _observed_at(value: object) -> str:
    if not isinstance(value, str):
        raise ConformanceError("observed_at must be a timezone-aware ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ConformanceError("observed_at must be a timezone-aware ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ConformanceError("observed_at must be timezone-aware")
    return parsed.isoformat()


@dataclass(frozen=True, slots=True)
class BackendConformanceEvidence:
    """Bounded evidence emitted by one backend conformance run."""

    backend_id: str
    deployment_digest: str
    supported_operations: tuple[str, ...]
    verified_operations: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    observed_at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "backend_id", _identity(self.backend_id, "backend_id"))
        object.__setattr__(self, "deployment_digest", _digest(self.deployment_digest, "deployment_digest"))
        supported = _operations(self.supported_operations, "supported_operations")
        verified = _operations(self.verified_operations, "verified_operations")
        if not set(verified).issubset(supported):
            raise ConformanceError("verified_operations must be supported by the backend")
        object.__setattr__(self, "supported_operations", supported)
        object.__setattr__(self, "verified_operations", verified)
        if type(self.evidence_ids) is not tuple or not 1 <= len(self.evidence_ids) <= 256:
            raise ConformanceError("evidence_ids must be a bounded tuple")
        evidence_ids = tuple(_identity(value, "evidence_id") for value in self.evidence_ids)
        if len(set(evidence_ids)) != len(evidence_ids):
            raise ConformanceError("evidence_ids contains a duplicate")
        object.__setattr__(self, "evidence_ids", evidence_ids)
        object.__setattr__(self, "observed_at", _observed_at(self.observed_at))

    @property
    def digest(self) -> str:
        payload = {"schema": "sonder.backend-conformance.v1", **asdict(self)}
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"),
            ensure_ascii=True, allow_nan=False,
        ).encode("ascii")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class ConformanceResult:
    accepted: bool
    reason: str
    required_operations: tuple[str, ...]
    missing_operations: tuple[str, ...]
    unsupported_operations: tuple[str, ...]
    distributed: bool = False


def evaluate_conformance(
    evidence: BackendConformanceEvidence,
    *,
    distributed: bool = False,
) -> ConformanceResult:
    """Evaluate required backend operations without contacting a provider."""
    if not isinstance(evidence, BackendConformanceEvidence):
        raise ConformanceError("backend conformance evidence is required")
    required = set(REQUIRED_OPERATIONS)
    if distributed:
        required.add("shard_loss_recovery")
    required_tuple = tuple(sorted(required))
    missing = tuple(sorted(required - set(evidence.verified_operations)))
    unsupported = tuple(sorted(required - set(evidence.supported_operations)))
    if missing:
        reason = "distributed_operations_unverified" if distributed and missing == ("shard_loss_recovery",) else "required_operations_unverified"
        return ConformanceResult(False, reason, required_tuple, missing, unsupported, distributed)
    return ConformanceResult(True, "required_operations_verified", required_tuple, (), (), distributed)


def verify_deployment_conformance(deployment, evidence: BackendConformanceEvidence) -> ConformanceResult:
    """Bind evidence to one immutable deployment manifest and its topology."""
    from .model_deployment import ModelDeployment

    if not isinstance(deployment, ModelDeployment):
        raise ConformanceError("model deployment is required")
    if not isinstance(evidence, BackendConformanceEvidence):
        raise ConformanceError("backend conformance evidence is required")
    if evidence.backend_id != deployment.backend:
        return ConformanceResult(False, "backend_id_mismatch", (), (), (), deployment.is_multihost)
    if evidence.deployment_digest != deployment.digest:
        return ConformanceResult(False, "deployment_digest_mismatch", (), (), (), deployment.is_multihost)
    return evaluate_conformance(evidence, distributed=deployment.is_multihost)


__all__ = [
    "BackendConformanceEvidence",
    "ConformanceError",
    "ConformanceResult",
    "REQUIRED_OPERATIONS",
    "SUPPORTED_OPERATIONS",
    "evaluate_conformance",
    "verify_deployment_conformance",
]
