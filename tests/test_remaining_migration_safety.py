from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from sonder_runtime.application.persistence.migration_safety import (
    BackupVerificationError,
    FutureSchemaError,
    RestoreVerificationError,
    adopt_schema_epoch,
    decide_bridge_cleanup,
    prove_restore,
    verify_backup_before_migration,
)


class Verifier:
    def __init__(self, problems=()):
        self.problems = tuple(problems)

    def verify(self, path):
        return self.problems


def _proof(tmp_path):
    source = tmp_path / "memory.db"
    source.write_bytes(b"legacy-state")
    backup = tmp_path / "backup"
    backup.mkdir()
    (backup / "manifest.json").write_text("{}", encoding="utf-8")
    return source, backup


def test_backup_must_verify_before_migration(tmp_path):
    source, backup = _proof(tmp_path)
    with pytest.raises(BackupVerificationError, match="verification failed"):
        verify_backup_before_migration(backup, {"memory": source}, Verifier(["bad checksum"]))

    proof = verify_backup_before_migration(backup, {"memory": source}, Verifier())
    assert proof.complete
    assert proof.source_digests["memory"] == hashlib.sha256(b"legacy-state").hexdigest()


def test_restore_proof_is_independent_and_complete(tmp_path):
    source, backup = _proof(tmp_path)
    proof = verify_backup_before_migration(backup, {"memory": source}, Verifier())
    restored = tmp_path / "restored"
    restored.mkdir()
    restored_file = restored / "memory.db"
    restored_file.write_bytes(source.read_bytes())
    result = prove_restore(proof, restored, {"memory": restored_file})
    assert result.restored_digests == proof.source_digests

    restored_file.write_bytes(b"corrupt")
    with pytest.raises(RestoreVerificationError, match="differs"):
        prove_restore(proof, restored, {"memory": restored_file})


def test_epoch_adoption_refuses_future_schema_and_binds_receipt(tmp_path):
    source, backup = _proof(tmp_path)
    proof = verify_backup_before_migration(backup, {"memory": source}, Verifier())
    receipt = {"epoch": 2, "source_version": "bridge"}
    adoption = adopt_schema_epoch(
        source_epoch=1, target_epoch=2, source_version="bridge",
        backup_proof=proof, receipt=receipt,
    )
    assert adoption.target_epoch == 2
    assert len(adoption.receipt_digest) == 64
    with pytest.raises(FutureSchemaError):
        adopt_schema_epoch(source_epoch=3, target_epoch=2, source_version="new",
                           backup_proof=proof, receipt=receipt)


def test_bridge_cleanup_is_denied_until_all_proofs_exist(tmp_path):
    source, backup = _proof(tmp_path)
    proof = verify_backup_before_migration(backup, {"memory": source}, Verifier())
    denied = decide_bridge_cleanup(adoption=None, backup_proof=proof, restore_proof=None,
                                   receipt_present=False, bridge_tests_passed=True)
    assert not denied.allowed
    restored = tmp_path / "restored"
    restored.mkdir()
    restored_file = restored / "memory.db"
    restored_file.write_bytes(source.read_bytes())
    restore = prove_restore(proof, restored, {"memory": restored_file})
    adoption = adopt_schema_epoch(source_epoch=1, target_epoch=2, source_version="bridge",
                                  backup_proof=proof, receipt={"epoch": 2})
    allowed = decide_bridge_cleanup(adoption=adoption, backup_proof=proof,
                                    restore_proof=restore, receipt_present=True,
                                    bridge_tests_passed=True)
    assert allowed.allowed
