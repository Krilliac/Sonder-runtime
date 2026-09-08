"""Real disk bytes, durable transfer receipts, and trusted scope boundaries."""

from dataclasses import replace
import hashlib
import time
import pytest

from sonder_runtime.application.artifacts.transfer import (
    ArtifactTransferService,
    TransferGrant,
    TransferLimits,
    TransferError,
)
from sonder_runtime.adapters.persistence.artifact_transfer import (
    SQLiteArtifactTransferStore,
)
from sonder_runtime.application.context import local_owner_context


def digest(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def transfers(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(
        "sonder_runtime.adapters.filesystem.file_ops.allowed_roots", lambda: [workspace]
    )
    context = local_owner_context(
        correlation_id="artifact-test", workspace_roots=(workspace,)
    )
    grant = TransferGrant(
        "owner",
        "project-a",
        "receiver",
        "grant-a",
        1,
        time.time() + 3600,
        True,
        True,
        128 * 1024 * 1024,
        300 * 1024 * 1024,
    )
    store = SQLiteArtifactTransferStore(tmp_path / "private")
    service = ArtifactTransferService(store, authorizer=lambda context, action: grant)
    yield service, store, grant, context
    service.close()


def begin(service, context, data, command="begin"):
    return service.begin_upload(
        dict(
            sha256=digest(data),
            size_bytes=len(data),
            media_type="application/octet-stream",
        ),
        command,
        context,
    )


def sealed(service, context, transfer_id):
    service.seal_upload(transfer_id, "seal", context)
    until = time.monotonic() + 15
    while time.monotonic() < until:
        receipt = service.inspect_upload(transfer_id, context)
        if receipt["state"] != "verifying":
            return receipt
        time.sleep(0.01)
    pytest.fail("verification did not finish")


def test_upload_chunks_replay_seal_and_range(transfers):
    service, store, grant, context = transfers
    data = b"binary\x00\xff" * 4096
    receipt = begin(service, context, data)
    assert begin(service, context, data) == receipt
    tid = receipt["transfer_id"]
    ack = service.append_chunk(tid, 0, digest(data), data, context)
    assert service.append_chunk(tid, 0, digest(data), data, context) == ack
    done = sealed(service, context, tid)
    assert done["state"] == "sealed", done
    result = service.read_range(done["artifact"]["artifact_id"], 7, 29, context)
    assert result.body == data[7:36] and result.chunk_sha256 == digest(result.body)
    assert result.sha256 == digest(data) and result.size_bytes == len(data)
    assert service.seal_upload(tid, "seal", context)["artifact"] == done["artifact"]


def test_wrong_scope_and_revocation_cannot_disclose_existing(transfers):
    service, store, grant, context = transfers
    receipt = begin(service, context, b"x")
    service.authorizer = lambda context, action: replace(grant, project_id="other")
    with pytest.raises(TransferError, match="NOT_FOUND"):
        service.inspect_upload(receipt["transfer_id"], context)
    service.authorizer = lambda context, action: replace(grant, principal_id="victim")
    with pytest.raises(TransferError, match="FORBIDDEN"):
        begin(service, context, b"x", "wrong-principal")
    service.authorizer = None
    with pytest.raises(TransferError, match="UNAVAILABLE"):
        begin(service, context, b"x")


def test_conflicts_corruption_and_quota(transfers):
    service, store, grant, context = transfers
    receipt = begin(service, context, b"abc")
    with pytest.raises(TransferError, match="CONFLICT"):
        begin(service, context, b"def")
    with pytest.raises(TransferError, match="DIGEST"):
        service.append_chunk(receipt["transfer_id"], 0, digest(b"bad"), b"abc", context)
    with pytest.raises(TransferError, match="OFFSET"):
        service.append_chunk(receipt["transfer_id"], 1, digest(b"bc"), b"bc", context)
    service.authorizer = lambda context, action: replace(grant, quota_bytes=6)
    with pytest.raises(TransferError, match="QUOTA"):
        begin(service, context, b"z", "over")


def test_restart_reconciles_chunk_file_sql_gap(transfers, monkeypatch):
    service, store, grant, context = transfers
    data = b"gap" * 100
    tid = begin(service, context, data)["transfer_id"]
    with monkeypatch.context() as patch:
        patch.setattr(
            store,
            "_after_chunk_publish",
            lambda: (_ for _ in ()).throw(RuntimeError("crash")),
        )
        with pytest.raises(RuntimeError):
            service.append_chunk(tid, 0, digest(data), data, context)
    reopened = ArtifactTransferService(
        SQLiteArtifactTransferStore(store.root), authorizer=lambda c, a: grant
    )
    try:
        assert reopened.inspect_upload(tid, context)["offset"] == 0
        reopened.append_chunk(tid, 0, digest(data), data, context)
        done = sealed(reopened, context, tid)
        assert (
            reopened.read_range(
                done["artifact"]["artifact_id"], 0, len(data), context
            ).body
            == data
        )
    finally:
        reopened.close()


def test_abort_releases_reservation_only_after_cleanup(transfers):
    service, store, grant, context = transfers
    service.authorizer = lambda c, a: replace(grant, quota_bytes=6)
    tid = begin(service, context, b"abc")["transfer_id"]
    service.append_chunk(tid, 0, digest(b"abc"), b"abc", context)
    service.abort_upload(tid, "abort", context)
    assert begin(service, context, b"xyz", "next")["state"] == "open"


def test_seal_recovers_published_blob_sql_gap(transfers, monkeypatch):
    service, store, grant, context = transfers
    data = b"publication-gap"
    tid = begin(service, context, data)["transfer_id"]
    service.append_chunk(tid, 0, digest(data), data, context)
    original = store._after_object_publish
    store._after_object_publish = lambda: (_ for _ in ()).throw(
        RuntimeError("power-loss")
    )
    service.seal_upload(tid, "seal", context)
    service.close()
    store._after_object_publish = original
    reopened = ArtifactTransferService(
        SQLiteArtifactTransferStore(store.root), authorizer=lambda c, a: grant
    )
    try:
        result = sealed(reopened, context, tid)
        assert result["state"] == "sealed", result
    finally:
        reopened.close()


def test_seal_recovers_cleanup_sql_gap(transfers, monkeypatch):
    service, store, grant, context = transfers
    data = b"cleanup-gap"
    tid = begin(service, context, data)["transfer_id"]
    service.append_chunk(tid, 0, digest(data), data, context)
    monkeypatch.setattr(
        store,
        "_after_stage_cleanup",
        lambda: (_ for _ in ()).throw(RuntimeError("power-loss")),
        raising=False,
    )
    service.seal_upload(tid, "seal", context)
    service.close()
    assert service.inspect_upload(tid, context)["state"] == "verifying"
    reopened = ArtifactTransferService(
        SQLiteArtifactTransferStore(store.root), authorizer=lambda c, a: grant
    )
    try:
        assert sealed(reopened, context, tid)["state"] == "sealed"
    finally:
        reopened.close()


def test_replayed_begin_rejects_corrupt_acknowledged_prefix(transfers):
    service, store, grant, context = transfers
    data = b"prefix"
    tid = begin(service, context, data)["transfer_id"]
    service.append_chunk(tid, 0, digest(data), data, context)
    path = store.root / grant.scope_id / tid / "0000000000000000"
    path.chmod(0o600)
    path.write_bytes(b"broken")
    with pytest.raises(TransferError, match="DIGEST"):
        begin(service, context, data)


def test_abort_cleanup_failure_retains_quota(transfers, monkeypatch):
    from sonder_runtime.application.compute_fabric.artifact_spool import (
        PrivateDirectoryAnchor,
    )

    service, store, grant, context = transfers
    service.authorizer = lambda c, a: replace(grant, quota_bytes=6)
    tid = begin(service, context, b"abc")["transfer_id"]
    service.append_chunk(tid, 0, digest(b"abc"), b"abc", context)
    with monkeypatch.context() as patch:
        patch.setattr(
            PrivateDirectoryAnchor,
            "unlink",
            lambda *a: (_ for _ in ()).throw(OSError("fixture")),
        )
        with pytest.raises(OSError):
            service.abort_upload(tid, "abort", context)
    with pytest.raises(TransferError, match="QUOTA"):
        begin(service, context, b"x", "blocked")
    service.abort_upload(tid, "abort", context)
    assert begin(service, context, b"xyz", "after")["state"] == "open"


def test_expired_staging_reaper_preserves_published_objects(transfers):
    service, store, grant, context = transfers
    first = begin(service, context, b"a")["transfer_id"]
    service.append_chunk(first, 0, digest(b"a"), b"a", context)
    done = sealed(service, context, first)
    second = begin(service, context, b"b", "staging")["transfer_id"]
    service.append_chunk(second, 0, digest(b"b"), b"b", context)
    with store._connection() as conn:
        conn.execute("UPDATE artifact_uploads SET expires=0")
    assert store.reap_expired() == 1
    assert (
        service.read_range(done["artifact"]["artifact_id"], 0, 1, context).body == b"a"
    )


def test_actual_process_exit_between_chunk_fsync_and_sql_commit(transfers):
    import subprocess
    import sys

    service, store, grant, context = transfers
    data = b"process-exit"
    tid = begin(service, context, data)["transfer_id"]
    script = """
import os,sys,time
from sonder_runtime.application.artifacts.transfer import TransferGrant
from sonder_runtime.adapters.persistence.artifact_transfer import SQLiteArtifactTransferStore
store=SQLiteArtifactTransferStore(sys.argv[1])
grant=TransferGrant('owner','project-a','receiver','grant-a',1,float(sys.argv[4]),True,True,134217728,314572800)
store._after_chunk_publish=lambda:os._exit(27)
store.append(sys.argv[2],0,sys.argv[3],b'process-exit',grant)
"""
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(store.root),
            tid,
            digest(data),
            str(grant.expires_at),
        ],
        timeout=20,
    )
    assert child.returncode == 27
    assert service.inspect_upload(tid, context)["offset"] == 0
    ack = service.append_chunk(tid, 0, digest(data), data, context)
    assert ack["next_offset"] == len(data)
    assert sealed(service, context, tid)["state"] == "sealed"


def test_concurrent_mutations_do_not_multiply_stage_reservation(transfers, monkeypatch):
    import threading

    service, store, grant, context = transfers
    data = b"race"
    tid = begin(service, context, data)["transfer_id"]
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(
        store, "_after_chunk_publish", lambda: (entered.set(), release.wait(5))
    )
    result = []
    worker = threading.Thread(
        target=lambda: result.append(
            service.append_chunk(tid, 0, digest(data), data, context)
        )
    )
    worker.start()
    assert entered.wait(5)
    try:
        other = SQLiteArtifactTransferStore(store.root)
        with pytest.raises(TransferError, match="BUSY"):
            other.append(tid, 0, digest(data), data, grant)
    finally:
        release.set()
        worker.join(5)
    assert len(result) == 1
    assert service.append_chunk(tid, 0, digest(data), data, context) == result[0]


def test_store_overlapping_model_roots_is_rejected(tmp_path, monkeypatch):
    writable = tmp_path / "writable"
    writable.mkdir()
    monkeypatch.setattr(
        "sonder_runtime.adapters.filesystem.file_ops.allowed_roots", lambda: [writable]
    )
    with pytest.raises(TransferError, match="UNSAFE_STORE"):
        SQLiteArtifactTransferStore(writable / "cas")


def test_live_grant_revocation_and_scope_masks(transfers):
    service, store, grant, context = transfers
    receipt = begin(service, context, b"data")
    for changed in (
        replace(grant, can_write=False),
        replace(grant, expires_at=0),
        replace(grant, revision=2),
    ):
        service.authorizer = lambda c, a: changed
        with pytest.raises(TransferError, match="FORBIDDEN"):
            service.append_chunk(
                receipt["transfer_id"], 0, digest(b"data"), b"data", context
            )


def test_terminal_commands_still_validate_identity(transfers):
    service, store, grant, context = transfers
    data = b"terminal"
    tid = begin(service, context, data)["transfer_id"]
    service.append_chunk(tid, 0, digest(data), data, context)
    sealed(service, context, tid)
    with pytest.raises(TransferError, match="CONFLICT"):
        service.seal_upload(tid, "different", context)
    with pytest.raises(TransferError, match="INVALID_COMMAND"):
        service.seal_upload(tid, {}, context)
    aborted = begin(service, context, b"x", "abort-source")["transfer_id"]
    first = service.abort_upload(aborted, "abort", context)
    assert service.abort_upload(aborted, "abort", context) == first
    with pytest.raises(TransferError, match="CONFLICT"):
        service.abort_upload(aborted, "changed", context)


@pytest.mark.parametrize("damage", ["corrupt", "missing"])
def test_duplicate_append_reverifies_acknowledged_bytes(transfers, damage):
    service, store, grant, context = transfers
    data = b"replay"
    tid = begin(service, context, data)["transfer_id"]
    service.append_chunk(tid, 0, digest(data), data, context)
    path = store.root / grant.scope_id / tid / "0000000000000000"
    path.chmod(0o600)
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b"broken")
    with pytest.raises(TransferError, match="DIGEST"):
        service.append_chunk(tid, 0, digest(data), data, context)


def test_sealed_append_replay_verifies_published_range(transfers):
    service, store, grant, context = transfers
    data = b"sealed-retry"
    tid = begin(service, context, data)["transfer_id"]
    ack = service.append_chunk(tid, 0, digest(data), data, context)
    sealed(service, context, tid)
    assert service.append_chunk(tid, 0, digest(data), data, context) == ack
    blob = store.root / grant.scope_id / tid / digest(data)
    blob.chmod(0o600)
    blob.write_bytes(b"x" * len(data))
    with pytest.raises(TransferError, match="DIGEST"):
        service.append_chunk(tid, 0, digest(data), data, context)


def test_aborted_append_cannot_replay_success_and_payload_stays_out_of_receipts(
    transfers, caplog
):
    service, store, grant, context = transfers
    data = b"unique-private-payload-never-in-control-events"
    tid = begin(service, context, data)["transfer_id"]
    ack = service.append_chunk(tid, 0, digest(data), data, context)
    aborted = service.abort_upload(tid, "abort", context)
    with pytest.raises(TransferError, match="STATE_CONFLICT"):
        service.append_chunk(tid, 0, digest(data), data, context)
    import json

    assert data.decode() not in json.dumps([ack, aborted])
    assert data.decode() not in caplog.text


def test_published_blob_recovery_revalidates_after_scan(transfers, monkeypatch):
    service, store, grant, context = transfers
    data = b"scan-grant"
    tid = begin(service, context, data)["transfer_id"]
    service.append_chunk(tid, 0, digest(data), data, context)
    with monkeypatch.context() as patch:
        patch.setattr(
            store,
            "_after_object_publish",
            lambda: (_ for _ in ()).throw(RuntimeError("gap")),
        )
        service.seal_upload(tid, "seal", context)
        service.close()
    original = store._verify_blob
    current = [grant]

    def revoke_after_scan(*args, **kwargs):
        original(*args, **kwargs)
        current[0] = replace(grant, revision=2)

    monkeypatch.setattr(store, "_verify_blob", revoke_after_scan)
    with pytest.raises(TransferError, match="FORBIDDEN"):
        store.seal(tid, grant, lambda: current[0])
    assert store.inspect(tid, grant)["state"] == "verifying"
