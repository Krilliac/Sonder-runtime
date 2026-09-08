"""Task 5 durable local journal and dispatch-fencing contracts.

These tests intentionally simulate a future peer with a local list only.  The
journal itself must never import or call a network client: Task 6 composes the
bounded attempt around these local primitives.
"""

from __future__ import annotations

from dataclasses import replace
import json
import multiprocessing
import sqlite3

import pytest

from sonder_runtime.adapters.persistence.artifact_mobility import (
    SQLiteArtifactMobilityJournal,
)
from sonder_runtime.application.artifacts.mobility import (
    ArtifactMobilityJournal,
    MAX_RECEIPT_TTL_SECONDS,
    MobilityImmutableFence,
    MobilityJournalError,
    MobilityOperationRequest,
    ReceiptCheckpoint,
)


def _request(**changes):
    values = {
        "source_owner_id": "source-owner-a",
        "source_scope_id": "1" * 64,
        "source_artifact_id": "2" * 32,
        "immutable_spec": {
            "sha256": "3" * 64,
            "size_bytes": 9,
            "media_type": "application/octet-stream",
        },
        "destination_label": "node-one",
        "destination_scope_id": "4" * 64,
        "credential_generation": "5" * 64,
        "destination_binding_hmac": "6" * 64,
        "receipt_ttl_seconds": 60,
    }
    values.update(changes)
    return MobilityOperationRequest(**values)


def _journal(tmp_path, *, now=1000.0, operation_id="a" * 32):
    repository = SQLiteArtifactMobilityJournal(tmp_path / "private-journal")
    service = ArtifactMobilityJournal(
        repository,
        clock=lambda: now,
        operation_id_factory=lambda: operation_id,
        receipt_capability_factory=lambda: "b" * 64,
    )
    return repository, service


def _operation(tmp_path, *, now=1000.0, operation_id="a" * 32, **changes):
    repository, service = _journal(tmp_path, now=now, operation_id=operation_id)
    operation = service.create_operation(
        _request(**changes), credential_material="c" * 48
    )
    return repository, service, operation


def _checkpoint(operation, *, state="open", expires_at=1060.0):
    return ReceiptCheckpoint(
        transfer_id="d" * 32,
        artifact_id="e" * 32 if state == "sealed" else None,
        state=state,
        offset=operation.immutable_spec["size_bytes"],
        chunk_bytes=65536,
        revision=1,
        expires_at=expires_at,
    )


def _try_lock_in_child(root, operation_id, ready, release):
    """Hold a real OS lock in a distinct interpreter for the lock test."""
    repository = SQLiteArtifactMobilityJournal(root)
    lock = repository.try_acquire_dispatch_lock(operation_id)
    ready.set()
    release.wait(10)
    lock.close()


def _paused_lease_in_child(root, operation_id, owner, ready, check, result):
    """Model a process paused after it owns both the OS lock and journal lease."""
    repository = SQLiteArtifactMobilityJournal(root)
    lock = repository.try_acquire_dispatch_lock(operation_id)
    try:
        lease = repository.acquire_dispatch(
            operation_id, owner, lock=lock, now=1000.0, lease_seconds=2
        )
        ready.set()
        if not check.wait(10):
            result.put("timeout")
            return
        try:
            repository.assert_current_lease(lease, lock=lock, now=1002.0)
        except MobilityJournalError as error:
            result.put(str(error))
        else:
            result.put("unexpected-current")
    finally:
        lock.close()


def _crash_with_lock_in_child(root, operation_id, ready):
    """Exit without releasing the handle; the OS must release the lock itself."""
    repository = SQLiteArtifactMobilityJournal(root)
    lock = repository.try_acquire_dispatch_lock(operation_id)
    ready.set()
    # Do not run Python finally blocks; this models abrupt process loss.
    import os

    os._exit(0)


def test_service_generates_immutable_operation_before_any_simulated_peer_work(tmp_path):
    repository, service = _journal(tmp_path)
    request = _request()
    peer_calls = []

    operation = service.create_operation(request, credential_material="c" * 48)

    assert operation.operation_id == "a" * 32
    assert operation.remote_command_id == "mobility-v1." + "4" * 16 + "." + "a" * 32
    assert (
        repository.load_operation(operation.operation_id, request.source_owner_id)
        == operation
    )
    assert peer_calls == []
    assert "operation_id" not in MobilityOperationRequest.__dataclass_fields__
    assert "remote_command_id" not in MobilityOperationRequest.__dataclass_fields__


def test_repository_rejects_direct_noncanonical_initial_operations(tmp_path):
    """Only a new ready intent may enter the lifecycle/tombstone store."""
    seed_repository, service = _journal(tmp_path / "seed")
    operation = service.create_operation(_request(), credential_material="c" * 48)
    target = SQLiteArtifactMobilityJournal(tmp_path / "target")
    candidates = (
        replace(
            operation,
            state="terminal_blocked",
            outcome_code="IMMUTABLE_FENCE",
        ),
        replace(
            operation,
            state="dispatching",
            attempt_epoch=1,
            lease_token="f" * 64,
            lease_expires_at=1002.0,
        ),
        replace(operation, receipt=_checkpoint(operation)),
        replace(operation, outcome_code="MOBILITY_PROTOCOL"),
        replace(operation, updated_at=1001.0),
        replace(
            operation,
            receipt_expires_at=operation.created_at + MAX_RECEIPT_TTL_SECONDS + 1,
        ),
    )

    for candidate in candidates:
        with pytest.raises(MobilityJournalError, match="INVALID_REQUEST"):
            target.create_operation(candidate)

    assert target.list_public_status(operation.source_owner_id) == ()
    assert not target.tombstone_exists(
        operation.source_owner_id,
        operation.destination_scope_id,
        operation.operation_id,
    )
    seed_repository.close()


def test_lifecycle_lease_cas_and_stale_epoch_token_are_fenced(tmp_path):
    repository, _, operation = _operation(tmp_path)
    owner = operation.source_owner_id

    with repository.try_acquire_dispatch_lock(operation.operation_id) as first_lock:
        first = repository.acquire_dispatch(
            operation.operation_id, owner, lock=first_lock, now=1000.0, lease_seconds=2
        )
        assert (
            repository.load_operation(operation.operation_id, owner).state
            == "dispatching"
        )
        assert (
            repository.renew_dispatch(
                first, lock=first_lock, now=1001.0, lease_seconds=2
            ).epoch
            == 1
        )
        assert (
            repository.transition_with_lease(
                first, "resumable", lock=first_lock, now=1001.5
            ).state
            == "resumable"
        )

    with repository.try_acquire_dispatch_lock(
        operation.operation_id
    ) as replacement_lock:
        replacement = repository.acquire_dispatch(
            operation.operation_id,
            owner,
            lock=replacement_lock,
            now=1002.0,
            lease_seconds=2,
        )
        assert replacement.epoch == 2
        with pytest.raises(MobilityJournalError, match="LEASE_LOST"):
            repository.renew_dispatch(
                first, lock=replacement_lock, now=1002.0, lease_seconds=2
            )
        with pytest.raises(MobilityJournalError, match="LEASE_LOST"):
            repository.transition_with_lease(
                first, "retryable_blocked", lock=replacement_lock, now=1002.0
            )

        assert (
            repository.transition_with_lease(
                replacement,
                "awaiting_seal",
                lock=replacement_lock,
                now=1002.5,
                receipt=_checkpoint(operation, state="verifying"),
            ).state
            == "awaiting_seal"
        )
    with repository.try_acquire_dispatch_lock(operation.operation_id) as third_lock:
        assert (
            repository.acquire_dispatch(
                operation.operation_id,
                owner,
                lock=third_lock,
                now=1003.0,
                lease_seconds=2,
            ).epoch
            == 3
        )


def test_expired_recovery_is_explicit_local_only_and_reopen_safe(tmp_path):
    repository, _, operation = _operation(tmp_path)
    with repository.try_acquire_dispatch_lock(operation.operation_id) as lock:
        lease = repository.acquire_dispatch(
            operation.operation_id,
            operation.source_owner_id,
            lock=lock,
            now=1000.0,
            lease_seconds=2,
        )
    assert lease.expires_at == 1002.0
    repository.close()

    reopened = SQLiteArtifactMobilityJournal(tmp_path / "private-journal")
    simulated_peer_calls = []
    assert (
        reopened.load_operation(operation.operation_id, operation.source_owner_id).state
        == "dispatching"
    )
    assert reopened.recover_expired_leases(now=1001.0) == ()
    assert (
        reopened.load_operation(operation.operation_id, operation.source_owner_id).state
        == "dispatching"
    )
    assert reopened.recover_expired_leases(now=1002.0) == (operation.operation_id,)
    assert (
        reopened.load_operation(operation.operation_id, operation.source_owner_id).state
        == "resumable"
    )
    assert simulated_peer_calls == []


def test_os_dispatch_lock_is_real_nonblocking_and_cross_process(tmp_path):
    repository, _, operation = _operation(tmp_path)
    context = multiprocessing.get_context("spawn")
    ready, release = context.Event(), context.Event()
    child = context.Process(
        target=_try_lock_in_child,
        args=(
            str(tmp_path / "private-journal"),
            operation.operation_id,
            ready,
            release,
        ),
    )
    child.start()
    try:
        assert ready.wait(10)
        with pytest.raises(MobilityJournalError, match="BUSY"):
            repository.try_acquire_dispatch_lock(operation.operation_id)
    finally:
        release.set()
        child.join(10)
        if child.is_alive():
            child.terminate()
            child.join(10)
    assert child.exitcode == 0
    with repository.try_acquire_dispatch_lock(operation.operation_id) as lock:
        assert lock.held


def test_paused_sender_cannot_overlap_replacement_after_lease_expiry(tmp_path):
    repository, _, operation = _operation(tmp_path)
    owner = operation.source_owner_id
    simulated_peer_calls = []
    first_lock = repository.try_acquire_dispatch_lock(operation.operation_id)
    try:
        stale = repository.acquire_dispatch(
            operation.operation_id, owner, lock=first_lock, now=1000.0, lease_seconds=2
        )
        assert repository.recover_expired_leases(now=1002.0) == (
            operation.operation_id,
        )
        with pytest.raises(MobilityJournalError, match="BUSY"):
            repository.try_acquire_dispatch_lock(operation.operation_id)
        with pytest.raises(MobilityJournalError, match="LEASE_LOST"):
            repository.assert_current_lease(stale, lock=first_lock, now=1002.0)
        assert simulated_peer_calls == []
    finally:
        first_lock.close()

    with repository.try_acquire_dispatch_lock(
        operation.operation_id
    ) as replacement_lock:
        fresh = repository.acquire_dispatch(
            operation.operation_id,
            owner,
            lock=replacement_lock,
            now=1002.1,
            lease_seconds=2,
        )
        repository.assert_current_lease(fresh, lock=replacement_lock, now=1002.1)
        simulated_peer_calls.append("one future call")
        assert replacement_lock.held
        assert fresh.epoch == 2
    assert simulated_peer_calls == ["one future call"]


def test_paused_process_expiry_fence_uses_the_os_lock_not_only_thread_state(tmp_path):
    repository, _, operation = _operation(tmp_path)
    context = multiprocessing.get_context("spawn")
    ready, check = context.Event(), context.Event()
    result = context.Queue()
    child = context.Process(
        target=_paused_lease_in_child,
        args=(
            str(tmp_path / "private-journal"),
            operation.operation_id,
            operation.source_owner_id,
            ready,
            check,
            result,
        ),
    )
    child.start()
    try:
        assert ready.wait(10)
        assert repository.recover_expired_leases(now=1002.0) == (
            operation.operation_id,
        )
        with pytest.raises(MobilityJournalError, match="BUSY"):
            repository.try_acquire_dispatch_lock(operation.operation_id)
        check.set()
        assert result.get(timeout=10) == "LEASE_LOST"
    finally:
        check.set()
        child.join(10)
        if child.is_alive():
            child.terminate()
            child.join(10)
    assert child.exitcode == 0
    with repository.try_acquire_dispatch_lock(operation.operation_id) as lock:
        replacement = repository.acquire_dispatch(
            operation.operation_id,
            operation.source_owner_id,
            lock=lock,
            now=1002.1,
            lease_seconds=2,
        )
    assert replacement.epoch == 2


def test_os_lock_is_released_after_an_abrupt_process_exit(tmp_path):
    repository, _, operation = _operation(tmp_path)
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    child = context.Process(
        target=_crash_with_lock_in_child,
        args=(str(tmp_path / "private-journal"), operation.operation_id, ready),
    )
    child.start()
    assert ready.wait(10)
    child.join(10)
    assert child.exitcode == 0
    with repository.try_acquire_dispatch_lock(operation.operation_id) as lock:
        assert lock.held


def test_lease_mutation_requires_a_continuously_held_operation_lock(tmp_path):
    repository, _, operation = _operation(tmp_path)
    with pytest.raises(MobilityJournalError, match="LOCK_REQUIRED"):
        repository.acquire_dispatch(
            operation.operation_id,
            operation.source_owner_id,
            now=1000.0,
            lease_seconds=2,
        )
    assert (
        repository.load_operation(
            operation.operation_id, operation.source_owner_id
        ).state
        == "ready"
    )


@pytest.mark.parametrize(
    "changes",
    (
        {"source_owner_id": "source-owner-b"},
        {"source_scope_id": "7" * 64},
        {"credential_generation": "8" * 64},
        {"destination_binding_hmac": "9" * 64},
    ),
)
def test_changed_immutable_fence_is_terminal_and_cannot_resume(tmp_path, changes):
    repository, _, operation = _operation(tmp_path)
    current = _request(**changes)
    fence = MobilityImmutableFence.from_request(current)

    with repository.try_acquire_dispatch_lock(operation.operation_id) as lock:
        lease = repository.acquire_dispatch(
            operation.operation_id,
            operation.source_owner_id,
            lock=lock,
            now=1000.0,
            lease_seconds=2,
        )
        with pytest.raises(MobilityJournalError, match="IMMUTABLE_FENCE"):
            repository.assert_immutable_fence(lease, fence, lock=lock, now=1001.0)
    persisted = repository.load_operation(
        operation.operation_id, operation.source_owner_id
    )
    assert persisted.state == "terminal_blocked"
    assert repository.tombstone_exists(
        operation.source_owner_id,
        operation.destination_scope_id,
        operation.operation_id,
    )
    with repository.try_acquire_dispatch_lock(operation.operation_id) as terminal_lock:
        with pytest.raises(MobilityJournalError, match="TERMINAL"):
            repository.acquire_dispatch(
                operation.operation_id,
                operation.source_owner_id,
                lock=terminal_lock,
                now=1002.0,
                lease_seconds=2,
            )


def test_receipt_pruning_keeps_permanent_tombstone_and_direct_reuse_fails(tmp_path):
    repository, _, operation = _operation(tmp_path)
    with repository.try_acquire_dispatch_lock(operation.operation_id) as lock:
        lease = repository.acquire_dispatch(
            operation.operation_id,
            operation.source_owner_id,
            lock=lock,
            now=1000.0,
            lease_seconds=2,
        )
        sealed = repository.transition_with_lease(
            lease,
            "sealed",
            lock=lock,
            now=1001.0,
            receipt=_checkpoint(operation, state="sealed", expires_at=1060.0),
        )
    assert sealed.state == "sealed"
    assert repository.tombstone_exists(
        operation.source_owner_id,
        operation.destination_scope_id,
        operation.operation_id,
    )
    with pytest.raises(MobilityJournalError, match="RECEIPT_LIVE"):
        repository.prune_receipt_keep_tombstone(
            operation.operation_id, operation.source_owner_id, now=1059.0
        )
    repository.prune_receipt_keep_tombstone(
        operation.operation_id, operation.source_owner_id, now=1060.0
    )
    with pytest.raises(MobilityJournalError, match="NOT_FOUND"):
        repository.load_operation(operation.operation_id, operation.source_owner_id)
    with pytest.raises(MobilityJournalError, match="NO_REUSE"):
        repository.create_operation(operation)
    repository.close()
    reopened = SQLiteArtifactMobilityJournal(tmp_path / "private-journal")
    assert reopened.tombstone_exists(
        operation.source_owner_id,
        operation.destination_scope_id,
        operation.operation_id,
    )
    with pytest.raises(MobilityJournalError, match="NO_REUSE"):
        reopened.create_operation(operation)
    database = tmp_path / "private-journal" / "artifact-mobility.sqlite"
    with sqlite3.connect(database) as connection:
        dump = "\n".join(connection.iterdump())
    assert operation.destination_binding_hmac not in dump
    assert operation.protected_receipt_capability not in dump


def test_private_record_and_public_projection_redact_all_sensitive_material(tmp_path):
    hmac_value = "6" * 64
    raw_origin = "https://destination.example:443/private"
    raw_pin = "pinned-leaf-certificate-value"
    raw_credential = "credential-material-should-never-persist"
    raw_path = r"C:\\private\\payload.bin"
    raw_payload = "payload-sentinel"
    raw_exception = "exception-sentinel"
    repository, service = _journal(tmp_path)
    operation = service.create_operation(
        _request(destination_binding_hmac=hmac_value), credential_material="c" * 48
    )
    status = repository.public_status(operation.operation_id, operation.source_owner_id)

    public = json.dumps(status, sort_keys=True)
    for value in (
        hmac_value,
        raw_origin,
        raw_pin,
        raw_credential,
        raw_path,
        raw_payload,
        raw_exception,
    ):
        assert value not in repr(operation)
        assert value not in repr(status)
        assert value not in public

    database = tmp_path / "private-journal" / "artifact-mobility.sqlite"
    with sqlite3.connect(database) as connection:
        dump = "\n".join(connection.iterdump())
    for value in (
        raw_origin,
        raw_pin,
        raw_credential,
        raw_path,
        raw_payload,
        raw_exception,
    ):
        assert value not in dump
    assert hmac_value in dump  # private comparison material is intentionally durable.


def test_internal_value_objects_keep_private_ids_and_tokens_out_of_repr(tmp_path):
    repository, _, operation = _operation(tmp_path)
    request = _request()
    fence = MobilityImmutableFence.from_request(request)
    checkpoint = _checkpoint(operation)
    with repository.try_acquire_dispatch_lock(operation.operation_id) as lock:
        lease = repository.acquire_dispatch(
            operation.operation_id,
            operation.source_owner_id,
            lock=lock,
            now=1000.0,
            lease_seconds=2,
        )
    for value in (
        request.source_scope_id,
        request.destination_scope_id,
        request.credential_generation,
        request.destination_binding_hmac,
        checkpoint.transfer_id,
        lease.operation_id,
        lease.source_owner_id,
        lease.token,
    ):
        assert value not in repr(request)
        assert value not in repr(fence)
        assert value not in repr(checkpoint)
        assert value not in repr(lease)


def test_receipt_capability_is_protected_at_rest_and_requires_current_key_material(
    tmp_path,
):
    repository, service = _journal(tmp_path)
    operation = service.create_operation(_request(), credential_material="c" * 48)
    version, nonce, ciphertext = operation.protected_receipt_capability.split(".")
    assert version == "v1"
    assert len(nonce) == 24
    assert len(ciphertext) == 96
    assert (
        service.receipt_capability_for(operation, credential_material="c" * 48)
        == "b" * 64
    )
    with pytest.raises(MobilityJournalError, match="IMMUTABLE_FENCE"):
        service.receipt_capability_for(operation, credential_material="d" * 48)
    database = tmp_path / "private-journal" / "artifact-mobility.sqlite"
    with sqlite3.connect(database) as connection:
        dump = "\n".join(connection.iterdump())
    assert "b" * 64 not in dump
    assert "c" * 48 not in dump

    tampered = operation.protected_receipt_capability[:-1] + (
        "0" if operation.protected_receipt_capability[-1] != "0" else "1"
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE mobility_operations SET protected_receipt_capability=? WHERE operation_id=?",
            (tampered, operation.operation_id),
        )
    with pytest.raises(MobilityJournalError, match="IMMUTABLE_FENCE"):
        service.receipt_capability_for(
            repository.load_operation(
                operation.operation_id, operation.source_owner_id
            ),
            credential_material="c" * 48,
        )


def test_credential_rejection_is_value_free_before_any_journal_mutation(tmp_path):
    repository, service = _journal(tmp_path)
    malformed = "https://credential.example:443/secret-value"
    with pytest.raises(MobilityJournalError, match="INVALID_CREDENTIAL") as error:
        service.create_operation(_request(), credential_material=malformed)
    assert malformed not in str(error.value)
    assert malformed not in repr(error.value)
    assert repository.list_public_status("source-owner-a") == ()


def test_invalid_private_values_and_owner_mismatch_are_stable_and_nonprojecting(
    tmp_path,
):
    repository, _, operation = _operation(tmp_path)
    with pytest.raises(MobilityJournalError, match="FORBIDDEN"):
        repository.load_operation(operation.operation_id, "source-owner-b")
    assert repository.load_operation_for_fencing(operation.operation_id) == operation
    with pytest.raises(MobilityJournalError, match="INVALID_REQUEST"):
        MobilityOperationRequest(
            **{
                **_request().__dict__,
                "destination_binding_hmac": "https://not-a-hmac.example:443/secret",
            }
        )
    assert "private-journal" not in repr(repository)
