from datetime import datetime, timedelta, timezone

import pytest

from sonder_runtime.application.artifacts.immutable_manifest import (
    ArtifactManifestBuilder, ArtifactRecord, ImmutableReference, RetentionPolicy,
    SpillMetadata, bounded_range,
)


def test_manifest_contains_full_digests_and_references_are_immutable():
    first = ArtifactRecord.from_bytes("b", b"two", media_type="text/plain")
    second = ArtifactRecord.from_bytes("a", b"one")
    manifest = ArtifactManifestBuilder(metadata={"owner": "session"}).build([first, second])
    assert len(manifest.digest) == 64
    assert tuple(item.artifact_id for item in manifest.entries) == ("a", "b")
    reference = manifest.reference("a")
    assert isinstance(reference, ImmutableReference)
    assert reference.manifest_digest == manifest.digest
    with pytest.raises((AttributeError, TypeError)):
        reference.digest = "f" * 64  # type: ignore[misc]


def test_manifest_digest_changes_when_payload_or_retention_changes():
    base = ArtifactRecord.from_bytes("a", b"same")
    later = ArtifactRecord.from_bytes(
        "a", b"same", retention=RetentionPolicy(datetime.now(timezone.utc) + timedelta(hours=1))
    )
    assert ArtifactManifestBuilder().build([base]).digest != ArtifactManifestBuilder().build([later]).digest


def test_spill_metadata_and_range_reads_are_bounded():
    record = ArtifactRecord.from_bytes("spill-artifact", b"0123456789")
    metadata = SpillMetadata("spill-1", record, max_bytes=32, preview_bytes=4)
    assert metadata.range_readable is True
    assert bounded_range(b"0123456789", offset=2, length=4, max_bytes=4) == b"2345"
    with pytest.raises(ValueError):
        bounded_range(b"0123456789", length=5, max_bytes=4)


def test_retention_is_explicit_and_expiry_is_deterministic():
    deadline = datetime(2026, 1, 1, tzinfo=timezone.utc)
    policy = RetentionPolicy(deadline, max_reads=2)
    assert not policy.expired(now=deadline - timedelta(seconds=1), reads=1)
    assert policy.expired(now=deadline - timedelta(seconds=1), reads=2)
    assert policy.expired(now=deadline, reads=0)
