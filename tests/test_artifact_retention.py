"""Reference-aware artifact cache retention policy tests."""
from datetime import datetime, timedelta, timezone

import pytest

from sonder_runtime.domain.artifact_retention import (
    ArtifactCacheEntry,
    ArtifactCachePage,
    ArtifactGcAction,
    ArtifactGcPolicy,
    ArtifactGcReason,
    ArtifactReference,
    ArtifactReferenceKind,
    ArtifactReferencePage,
    ArtifactReferenceState,
    ArtifactTombstone,
    ArtifactTombstoneKind,
    ArtifactTombstonePage,
    plan_artifact_gc,
)


NOW = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


def entry(
    artifact_id: str = "artifact-a",
    digest: str = DIGEST_A,
    version: str = "model-r1",
    *,
    last_accessed_at: datetime = NOW - timedelta(hours=2),
    retain_until: datetime | None = None,
    revision: int = 7,
    size_bytes: int = 128,
) -> ArtifactCacheEntry:
    return ArtifactCacheEntry(
        artifact_id,
        digest,
        version,
        size_bytes,
        last_accessed_at,
        retain_until,
        revision,
    )


def cache_page(*items: ArtifactCacheEntry, complete: bool = True, next_cursor: str | None = None, revision: int = 3) -> ArtifactCachePage:
    return ArtifactCachePage(tuple(items), complete=complete, next_cursor=next_cursor, revision=revision)


def reference(
    item: ArtifactCacheEntry,
    *,
    owner_id: str = "job-1",
    owner_kind: ArtifactReferenceKind = ArtifactReferenceKind.JOB,
    state: ArtifactReferenceState = ArtifactReferenceState.LIVE,
    lease_expires_at: datetime | None = NOW + timedelta(minutes=15),
    digest: str | None = None,
    version: str | None = None,
    revision: int = 2,
) -> ArtifactReference:
    return ArtifactReference(
        owner_id,
        owner_kind,
        state,
        item.artifact_id,
        digest if digest is not None else item.digest,
        version if version is not None else item.version,
        revision,
        lease_expires_at,
    )


def reference_page(*items: ArtifactReference, complete: bool = True, next_cursor: str | None = None, revision: int = 4) -> ArtifactReferencePage:
    return ArtifactReferencePage(tuple(items), complete=complete, next_cursor=next_cursor, revision=revision)


def tombstone(
    item: ArtifactCacheEntry,
    kind: ArtifactTombstoneKind,
    *,
    digest: str | None = None,
    version: str | None = None,
    revision: int = 8,
) -> ArtifactTombstone:
    return ArtifactTombstone(
        item.artifact_id,
        digest if digest is not None else item.digest,
        version if version is not None else item.version,
        kind,
        revision,
        NOW - timedelta(minutes=5),
        "operator-approved cache lifecycle decision",
    )


def tombstone_page(*items: ArtifactTombstone, complete: bool = True, next_cursor: str | None = None, revision: int = 5) -> ArtifactTombstonePage:
    return ArtifactTombstonePage(tuple(items), complete=complete, next_cursor=next_cursor, revision=revision)


def plan(item: ArtifactCacheEntry, *, references=(), tombstones=(), policy=None, cache_complete=True, references_complete=True, tombstones_complete=True):
    return plan_artifact_gc(
        cache_page(item, complete=cache_complete),
        reference_page(*references, complete=references_complete),
        tombstone_page(*tombstones, complete=tombstones_complete),
        now=NOW,
        policy=policy,
    )


def test_unreferenced_expired_entry_is_deleted_with_digest_bound_tombstone():
    result = plan(entry())

    assert result.complete is True
    decision = result.decisions[0]
    assert decision.action is ArtifactGcAction.DELETE
    assert decision.reason is ArtifactGcReason.ELIGIBLE
    assert decision.tombstone is not None
    assert decision.tombstone.kind is ArtifactTombstoneKind.DELETION
    assert decision.tombstone.artifact_id == "artifact-a"
    assert decision.tombstone.digest == DIGEST_A
    assert decision.tombstone.version == "model-r1"


def test_retention_window_pins_recent_entry():
    result = plan(entry(last_accessed_at=NOW - timedelta(minutes=10)))

    decision = result.decisions[0]
    assert decision.action is ArtifactGcAction.RETAIN
    assert decision.reason is ArtifactGcReason.RETENTION_WINDOW
    assert decision.tombstone is None


def test_explicit_future_retention_deadline_pins_entry():
    result = plan(entry(retain_until=NOW + timedelta(seconds=1)))

    assert result.decisions[0].reason is ArtifactGcReason.RETENTION_WINDOW
    assert result.decisions[0].action is ArtifactGcAction.RETAIN


def test_live_job_reference_pins_exact_artifact_generation():
    item = entry()
    result = plan(item, references=(reference(item),))

    decision = result.decisions[0]
    assert decision.action is ArtifactGcAction.RETAIN
    assert decision.reason is ArtifactGcReason.LIVE_JOB_REFERENCE


def test_live_deployment_reference_pins_exact_artifact_generation():
    item = entry()
    result = plan(
        item,
        references=(
            reference(
                item,
                owner_id="deployment-1",
                owner_kind=ArtifactReferenceKind.DEPLOYMENT,
            ),
        ),
    )

    assert result.decisions[0].reason is ArtifactGcReason.LIVE_DEPLOYMENT_REFERENCE


def test_live_reference_with_digest_or_version_mismatch_defers_deletion():
    item = entry()
    mismatched = reference(item, digest=DIGEST_B, version="model-r2")
    result = plan(item, references=(mismatched,))

    decision = result.decisions[0]
    assert decision.action is ArtifactGcAction.DEFER
    assert decision.reason is ArtifactGcReason.REFERENCE_IDENTITY_MISMATCH
    assert decision.tombstone is None


def test_expired_live_lease_defers_until_owner_releases_reference():
    item = entry()
    expired = reference(item, lease_expires_at=NOW)
    result = plan(item, references=(expired,))

    assert result.decisions[0].action is ArtifactGcAction.DEFER
    assert result.decisions[0].reason is ArtifactGcReason.REFERENCE_LEASE_EXPIRED


def test_released_reference_does_not_pin_entry():
    item = entry()
    released = reference(item, state=ArtifactReferenceState.RELEASED, lease_expires_at=NOW)
    result = plan(item, references=(released,))

    assert result.decisions[0].action is ArtifactGcAction.DELETE
    assert result.decisions[0].reason is ArtifactGcReason.ELIGIBLE


def test_incomplete_reference_scan_defers_every_candidate():
    result = plan(entry(), references_complete=False)

    assert result.complete is False
    assert result.decisions[0].action is ArtifactGcAction.DEFER
    assert result.decisions[0].reason is ArtifactGcReason.REFERENCE_SCAN_INCOMPLETE


def test_retention_tombstone_pins_entry():
    item = entry()
    hold = tombstone(item, ArtifactTombstoneKind.RETENTION)
    result = plan(item, tombstones=(hold,))

    decision = result.decisions[0]
    assert decision.action is ArtifactGcAction.RETAIN
    assert decision.reason is ArtifactGcReason.RETENTION_TOMBSTONE
    assert decision.tombstone == hold


def test_deletion_tombstone_reasserts_delete_without_resurrection():
    item = entry()
    deleted = tombstone(item, ArtifactTombstoneKind.DELETION)
    result = plan(item, tombstones=(deleted,))

    decision = result.decisions[0]
    assert decision.action is ArtifactGcAction.DELETE
    assert decision.reason is ArtifactGcReason.DELETION_TOMBSTONE
    assert decision.tombstone == deleted


def test_tombstone_identity_mismatch_defers_instead_of_authorizing_delete():
    item = entry()
    stale = tombstone(item, ArtifactTombstoneKind.DELETION, digest=DIGEST_B, version="model-r2")
    result = plan(item, tombstones=(stale,))

    assert result.decisions[0].action is ArtifactGcAction.DEFER
    assert result.decisions[0].reason is ArtifactGcReason.TOMBSTONE_IDENTITY_MISMATCH


def test_incomplete_tombstone_scan_defers_every_candidate():
    result = plan(entry(), tombstones_complete=False)

    assert result.complete is False
    assert result.decisions[0].action is ArtifactGcAction.DEFER
    assert result.decisions[0].reason is ArtifactGcReason.TOMBSTONE_SCAN_INCOMPLETE


def test_candidate_scan_is_bounded_and_reports_cursor():
    first = entry("artifact-a")
    second = entry("artifact-b", digest=DIGEST_B)
    result = plan_artifact_gc(
        cache_page(first, second, next_cursor="artifact-b"),
        reference_page(),
        tombstone_page(),
        now=NOW,
        policy=ArtifactGcPolicy(max_candidates=1),
    )

    assert result.complete is False
    assert result.scanned_candidates == 1
    assert result.next_cursor == "artifact-b"
    assert len(result.decisions) == 1


def test_candidates_are_returned_in_deterministic_identity_order():
    first = entry("artifact-b", digest=DIGEST_B)
    second = entry("artifact-a")
    result = plan_artifact_gc(
        cache_page(first, second),
        reference_page(),
        tombstone_page(),
        now=NOW,
    )

    assert [item.artifact_id for item in result.decisions] == ["artifact-a", "artifact-b"]


def test_conflicting_duplicate_candidate_identity_defers_both_generations():
    first = entry("artifact-a", digest=DIGEST_A, version="model-r1")
    second = entry("artifact-a", digest=DIGEST_B, version="model-r2")
    result = plan_artifact_gc(
        cache_page(first, second),
        reference_page(),
        tombstone_page(),
        now=NOW,
    )

    assert len(result.decisions) == 2
    assert {decision.reason for decision in result.decisions} == {ArtifactGcReason.CANDIDATE_IDENTITY_CONFLICT}
    assert all(decision.action is ArtifactGcAction.DEFER for decision in result.decisions)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: entry(digest="A" * 64),
        lambda: entry(version=""),
        lambda: entry(size_bytes=-1),
    ],
)
def test_cache_entry_rejects_invalid_identity_or_bounds(factory):
    with pytest.raises(ValueError):
        factory()


def test_pages_and_policy_reject_unbounded_or_invalid_revisions():
    item = entry()
    with pytest.raises(ValueError):
        ArtifactCachePage((item,), revision=0)
    with pytest.raises(ValueError):
        ArtifactReferencePage((), complete=False, next_cursor=None, revision=0)
    with pytest.raises(ValueError):
        ArtifactTombstonePage((), complete=True, next_cursor="", revision=1)
    with pytest.raises(ValueError):
        ArtifactGcPolicy(max_candidates=0)
    with pytest.raises(ValueError):
        ArtifactGcPolicy(max_references=0)
