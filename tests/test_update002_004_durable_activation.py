from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

from sonder_runtime.adapters.updates.activation_journal import JsonActivationJournal
from sonder_runtime.application.updates.durable_activation import DurableActivationCoordinator
from sonder_runtime.application.updates.durable_activation import ActivationJournalEntry
from sonder_runtime.application.updates.release_evidence import (
    ActivationRecoveryError, ActivationRequest, ReleaseEvidencePackage,
    RollbackCompatibility, SbomComponent, SignedReleaseManifest, TestEvidence,
)


def _digest(data: bytes) -> str:
    return sha256(data).hexdigest()


def _package() -> ReleaseEvidencePackage:
    manifest = SignedReleaseManifest(
        "rel-2", "2.0", (("bundle", _digest(b"bundle")),), "key", "sig",
        (("python", "3.12"), ("tuf", "3.0")),
    )
    return ReleaseEvidencePackage.build(
        manifest=manifest, sbom=(SbomComponent("python", "3.12"),),
        tests=(TestEvidence("focused", 1),), migrations=(),
        rollback=RollbackCompatibility(("rel-1",), True, "restore-proof"),
    )


class Pointer:
    def __init__(self, value="rel-1", fail_restore=False):
        self.value, self.fail_restore = value, fail_restore

    def current(self): return self.value
    def commit(self, value):
        if self.fail_restore and value == "rel-1": raise OSError("pointer unavailable")
        self.value = value


class Helper:
    def __init__(self, fail=False, fail_rollback=False):
        self.fail, self.fail_rollback, self.calls = fail, fail_rollback, []

    def activate(self, request):
        self.calls.append(("activate", request))
        if self.fail: raise RuntimeError("activation failed")

    def rollback(self, request):
        self.calls.append(("rollback", request))
        if self.fail_rollback: raise RuntimeError("rollback failed")


def _request(package):
    return ActivationRequest("linux", "rel-1", "rel-2", package.package_digest, "nonce")


def _coordinator(pointer, helper, journal):
    return DurableActivationCoordinator(pointer, helper, journal, lambda *_: True)


def test_success_requires_exact_dependencies_and_persists_outcome(tmp_path: Path):
    package, journal = _package(), JsonActivationJournal(tmp_path / "activation.jsonl")
    coordinator = _coordinator(Pointer(), Helper(), journal)
    assert coordinator.activate("a1", _request(package), package,
                                observed_dependencies={"python": "3.12", "tuf": "3.0"}) == "rel-2"
    assert [entry.phase for entry in journal.entries()] == ["prepared", "activated"]
    with pytest.raises(ValueError, match="exact sealed"):
        coordinator.activate("a2", _request(package), package,
                             observed_dependencies={"python": "3.12"})


def test_activation_failure_rolls_back_helper_and_pointer_atomically(tmp_path: Path):
    package = _package()
    pointer, helper = Pointer(), Helper(fail=True)
    journal = JsonActivationJournal(tmp_path / "activation.jsonl")
    with pytest.raises(RuntimeError, match="activation failed"):
        _coordinator(pointer, helper, journal).activate(
            "a1", _request(package), package,
            observed_dependencies={"python": "3.12", "tuf": "3.0"})
    assert pointer.value == "rel-1"
    assert [call[0] for call in helper.calls] == ["activate", "rollback"]
    assert journal.entries()[-1].phase == "recovered"


def test_incomplete_rollback_is_fail_closed(tmp_path: Path):
    package = _package()
    pointer, helper = Pointer(fail_restore=True), Helper(fail=True, fail_rollback=True)
    journal = JsonActivationJournal(tmp_path / "activation.jsonl")
    with pytest.raises(ActivationRecoveryError):
        _coordinator(pointer, helper, journal).activate(
            "a1", _request(package), package,
            observed_dependencies={"python": "3.12", "tuf": "3.0"})
    assert journal.entries()[-1].phase == "recovery_failed"


def test_restart_recovers_prepared_activation_before_new_work(tmp_path: Path):
    journal = JsonActivationJournal(tmp_path / "activation.jsonl")
    package = _package()
    request = _request(package)
    journal.append(ActivationJournalEntry(
        "interrupted", "prepared", request.platform, request.current_release,
        request.target_release, request.release_evidence_digest,
        helper_nonce=request.helper_nonce,
    ))
    pointer, helper = Pointer(value="rel-2"), Helper()
    _coordinator(pointer, helper, journal)
    assert pointer.current() == "rel-1"
    assert journal.entries()[-1].phase == "recovered"
    assert [call[0] for call in helper.calls] == ["rollback"]


def test_journal_fsyncs_and_refuses_corrupt_record(tmp_path: Path, monkeypatch):
    import os

    journal_path = tmp_path / "activation.jsonl"
    journal = JsonActivationJournal(journal_path)
    synced = []
    real_fsync = os.fsync

    def observe_fsync(fd):
        synced.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", observe_fsync)
    journal.append(ActivationJournalEntry(
        "a1", "prepared", "linux", "rel-1", "rel-2", "evidence", helper_nonce="nonce",
    ))
    assert synced
    raw = journal_path.read_bytes().replace(b"rel-2", b"rel-3")
    journal_path.write_bytes(raw)
    with pytest.raises(ValueError, match="checksum|corrupt"):
        journal.entries()
