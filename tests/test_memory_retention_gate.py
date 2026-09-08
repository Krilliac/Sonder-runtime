"""Pure evidence gates for authoritative memory retention decisions."""
from datetime import datetime, timedelta, timezone

import pytest

from sonder_runtime.domain.memory.retention_gate import (
    ActiveMemoryReference,
    MemoryAcknowledgement,
    MemoryAcknowledgementKind,
    MemoryRecordIdentity,
    MemoryRetentionPolicy,
    MemoryRetentionReason,
    MemoryRetentionRequest,
    RetentionDecisionStatus,
    decide_memory_retention,
)


NOW = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)


def _record() -> MemoryRecordIdentity:
    return MemoryRecordIdentity("project-alpha", "fact-7", 3, "tombstone-3")


def _backup(*, holder: str = "backup-a", record: MemoryRecordIdentity | None = None):
    record = record or _record()
    return MemoryAcknowledgement(
        "ack-" + holder,
        holder,
        MemoryAcknowledgementKind.BACKUP,
        record.project_scope,
        record.record_id,
        record.record_version,
        record.tombstone_id,
    )


def _replica(
    holder: str,
    *,
    record: MemoryRecordIdentity | None = None,
    acknowledgement_id: str | None = None,
):
    record = record or _record()
    return MemoryAcknowledgement(
        acknowledgement_id or "ack-" + holder,
        holder,
        MemoryAcknowledgementKind.REPLICA,
        record.project_scope,
        record.record_id,
        record.record_version,
        record.tombstone_id,
    )


def _request(**kwargs) -> MemoryRetentionRequest:
    values = {
        "record": _record(),
        "policy": MemoryRetentionPolicy(retain_until=NOW - timedelta(seconds=1)),
        "backup_acknowledgements": (_backup(),),
        "replica_acknowledgements": (_replica("node-2"),),
    }
    values.update(kwargs)
    return MemoryRetentionRequest(**values)


def test_matching_expired_record_is_allowed_with_durable_acknowledgements():
    decision = decide_memory_retention(_request(), now=NOW)

    assert decision.allowed
    assert decision.status is RetentionDecisionStatus.ALLOWED
    assert decision.reason_codes == ()
    assert decision.active_job_reference_count == 0
    assert decision.backup_acknowledgement_count == 1
    assert decision.replica_acknowledgement_count == 1
    assert len(decision.explanation) <= 512


def test_missing_expiry_fails_closed_even_when_acknowledgements_exist():
    decision = decide_memory_retention(
        _request(policy=MemoryRetentionPolicy()),
        now=NOW,
    )

    assert not decision.allowed
    assert MemoryRetentionReason.RETENTION_NOT_EXPIRED in decision.reason_codes


def test_unexpired_window_blocks_deletion_at_boundary():
    decision = decide_memory_retention(
        _request(
            policy=MemoryRetentionPolicy(
                retain_until=NOW + timedelta(microseconds=1)
            )
        ),
        now=NOW,
    )

    assert MemoryRetentionReason.RETENTION_NOT_EXPIRED in decision.reason_codes


def test_active_job_and_deployment_references_block_with_bounded_counts():
    record = _record()
    job = ActiveMemoryReference(
        "job-1",
        record.project_scope,
        record.record_id,
        record.record_version,
        record.tombstone_id,
    )
    deployment = ActiveMemoryReference(
        "deployment-1",
        record.project_scope,
        record.record_id,
        record.record_version,
        record.tombstone_id,
    )

    decision = decide_memory_retention(
        _request(
            active_job_references=(job,),
            active_deployment_references=(deployment,),
        ),
        now=NOW,
    )

    assert not decision.allowed
    assert decision.active_job_reference_count == 1
    assert decision.active_deployment_reference_count == 1
    assert MemoryRetentionReason.ACTIVE_JOB_REFERENCE in decision.reason_codes
    assert MemoryRetentionReason.ACTIVE_DEPLOYMENT_REFERENCE in decision.reason_codes


def test_reference_from_another_scope_blocks_without_becoming_a_live_match():
    record = _record()
    foreign = ActiveMemoryReference(
        "job-foreign",
        "project-other",
        record.record_id,
        record.record_version,
        record.tombstone_id,
    )

    decision = decide_memory_retention(
        _request(active_job_references=(foreign,)),
        now=NOW,
    )

    assert not decision.allowed
    assert decision.active_job_reference_count == 0
    assert MemoryRetentionReason.JOB_REFERENCE_IDENTITY_MISMATCH in decision.reason_codes


def test_backup_acknowledgement_is_required_by_default():
    decision = decide_memory_retention(
        _request(backup_acknowledgements=()),
        now=NOW,
    )

    assert not decision.allowed
    assert MemoryRetentionReason.BACKUP_ACKNOWLEDGEMENT_MISSING in decision.reason_codes


def test_replica_acknowledgement_count_requires_distinct_holders():
    decision = decide_memory_retention(
        _request(
            policy=MemoryRetentionPolicy(
                retain_until=NOW - timedelta(seconds=1),
                required_replica_acknowledgements=2,
            ),
            replica_acknowledgements=(
                _replica("node-2", acknowledgement_id="ack-node-2-a"),
                _replica("node-2", acknowledgement_id="ack-node-2-b"),
            ),
        ),
        now=NOW,
    )

    assert decision.replica_acknowledgement_count == 1
    assert MemoryRetentionReason.REPLICA_ACKNOWLEDGEMENT_MISSING in decision.reason_codes


def test_replica_acknowledgement_target_mismatch_blocks_even_if_policy_requires_none():
    foreign = MemoryRecordIdentity("project-other", "fact-7", 3, "tombstone-3")
    decision = decide_memory_retention(
        _request(
            policy=MemoryRetentionPolicy(
                retain_until=NOW - timedelta(seconds=1),
                required_replica_acknowledgements=0,
            ),
            replica_acknowledgements=(_replica("node-2", record=foreign),),
        ),
        now=NOW,
    )

    assert not decision.allowed
    assert MemoryRetentionReason.REPLICA_ACKNOWLEDGEMENT_MISMATCH in decision.reason_codes


def test_single_pc_policy_can_require_backup_without_a_replica():
    decision = decide_memory_retention(
        _request(
            policy=MemoryRetentionPolicy(
                retain_until=NOW - timedelta(seconds=1),
                required_replica_acknowledgements=0,
            ),
            replica_acknowledgements=(),
        ),
        now=NOW,
    )

    assert decision.allowed
    assert decision.replica_acknowledgement_count == 0


def test_wrong_acknowledgement_kind_is_not_durability_proof():
    backup = _backup()
    wrong_kind = MemoryAcknowledgement(
        backup.acknowledgement_id,
        backup.holder_id,
        MemoryAcknowledgementKind.REPLICA,
        backup.project_scope,
        backup.record_id,
        backup.record_version,
        backup.tombstone_id,
    )
    # Use a distinct ID so the request remains structurally valid.
    wrong_kind = MemoryAcknowledgement(
        "ack-wrong-kind",
        wrong_kind.holder_id,
        wrong_kind.kind,
        wrong_kind.project_scope,
        wrong_kind.record_id,
        wrong_kind.record_version,
        wrong_kind.tombstone_id,
    )
    decision = decide_memory_retention(
        _request(backup_acknowledgements=(wrong_kind,)),
        now=NOW,
    )

    assert not decision.allowed
    assert MemoryRetentionReason.BACKUP_ACKNOWLEDGEMENT_MISMATCH in decision.reason_codes
    assert MemoryRetentionReason.BACKUP_ACKNOWLEDGEMENT_MISSING in decision.reason_codes


def test_record_version_and_tombstone_are_part_of_acknowledgement_identity():
    record = _record()
    stale = MemoryRecordIdentity(
        record.project_scope,
        record.record_id,
        record.record_version - 1,
        record.tombstone_id,
    )
    decision = decide_memory_retention(
        _request(
            backup_acknowledgements=(_backup(record=stale),),
            replica_acknowledgements=(_replica("node-2", record=stale),),
        ),
        now=NOW,
    )

    assert not decision.allowed
    assert MemoryRetentionReason.BACKUP_ACKNOWLEDGEMENT_MISMATCH in decision.reason_codes
    assert MemoryRetentionReason.REPLICA_ACKNOWLEDGEMENT_MISMATCH in decision.reason_codes


def test_duplicate_ids_and_unbounded_evidence_are_rejected():
    with pytest.raises(ValueError, match="acknowledgement IDs"):
        MemoryRetentionRequest(
            _record(),
            MemoryRetentionPolicy(),
            backup_acknowledgements=(_backup(), _backup(holder="backup-a")),
        )

    with pytest.raises(ValueError, match="at most"):
        MemoryRetentionRequest(
            _record(),
            MemoryRetentionPolicy(),
            active_job_references=tuple(
                ActiveMemoryReference(
                    f"job-{index}",
                    "project-alpha",
                    "fact-7",
                    3,
                    "tombstone-3",
                )
                for index in range(257)
            ),
        )


def test_identity_validation_requires_version_tombstone_and_aware_time():
    with pytest.raises(ValueError, match="record_version"):
        MemoryRecordIdentity("project", "record", 0, "tombstone")
    with pytest.raises(ValueError, match="timezone-aware"):
        MemoryRetentionPolicy(retain_until=datetime(2026, 9, 5))
    with pytest.raises(ValueError, match="tombstone_id"):
        MemoryRecordIdentity("project", "record", 1, "")


def test_decision_serialization_is_bounded_and_preserves_exact_identity():
    decision = decide_memory_retention(
        _request(active_job_references=()),
        now=NOW,
    )

    serialized = decision.as_dict()
    assert serialized["record"] == _record().as_dict()
    assert serialized["status"] == "allowed"
    assert len(serialized["explanation"]) <= 512
