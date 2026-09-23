"""Immutable artifact manifest and bounded attachment primitives."""

from .immutable_manifest import (
    ArtifactManifest,
    ArtifactManifestBuilder,
    ArtifactRecord,
    ImmutableReference,
    RetentionPolicy,
    SpillMetadata,
    bounded_range,
)
from .readiness import (
    ArtifactReadiness,
    ArtifactReadinessBarrier,
    DEFAULT_MAX_AGE,
    READINESS_SCHEMA_VERSION,
)

__all__ = [
    "ArtifactManifest",
    "ArtifactManifestBuilder",
    "ArtifactRecord",
    "ImmutableReference",
    "RetentionPolicy",
    "SpillMetadata",
    "bounded_range",
    "ArtifactReadiness",
    "ArtifactReadinessBarrier",
    "READINESS_SCHEMA_VERSION",
    "DEFAULT_MAX_AGE",
]
