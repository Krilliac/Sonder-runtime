"""Task 6: one explicit, bounded outbound artifact dispatch service."""

from __future__ import annotations

import hashlib
import threading

import pytest

from sonder_runtime.adapters.persistence.artifact_mobility import (
    SQLiteArtifactMobilityJournal,
)
from sonder_runtime.application.artifacts import mobility
from sonder_runtime.application.artifacts.mobility_source import (
    MobilitySourceError,
    SourceArtifactRange,
)
from sonder_runtime.application.artifacts.transfer import (
    TransferError,
    recipient_mobility_attestation,
)


_SOURCE_ID = "2" * 32
_TRANSFER_ID = "d" * 32
_ARTIFACT_ID = "e" * 32
_CAPABILITY = "b" * 64
_CREDENTIAL = "peer-" + "c" * 32


def _context(**changes):
    values = {
        "source_owner_id": "source-owner-a",
        "source_scope_id": "1" * 64,
        "destination_label": "node-one",
        "destination_scope_id": "4" * 64,
        "credential_generation": "5" * 64,
        "destination_binding_hmac": "6" * 64,
        "credential_material": _CREDENTIAL,
        "attempt_lease_seconds": 2,
        "receipt_ttl_seconds": 60,
    }
    values.update(changes)
    return mobility.MobilityDispatchContext(**values)


class _ContextProvider:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


class _Clock:
    def __init__(self, value=1000.0):
        self.value = value

    def __call__(self):
        return self.value


class _Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.revoked = False
        self.inspections = 0
        self.reads = []

    @property
    def spec(self):
        return {
            "sha256": hashlib.sha256(self.data).hexdigest(),
            "size_bytes": len(self.data),
            "media_type": "application/octet-stream",
        }

    def inspect_sealed(self, source_artifact_id):
        self.inspections += 1
        if self.revoked:
            raise MobilitySourceError("FORBIDDEN")
        assert source_artifact_id == _SOURCE_ID
        return {"source_artifact_id": _SOURCE_ID, **self.spec}

    def read_range(self, source_artifact_id, offset, length):
        if self.revoked:
            raise MobilitySourceError("FORBIDDEN")
        assert source_artifact_id == _SOURCE_ID
        body = self.data[offset : offset + length]
        self.reads.append((offset, length))
        return SourceArtifactRange(
            source_artifact_id=_SOURCE_ID,
            sha256=self.spec["sha256"],
            size_bytes=len(self.data),
            offset=offset,
            body=body,
            chunk_sha256=hashlib.sha256(body).hexdigest(),
        )


class _Peer:
    def __init__(self, data: bytes, *, reader=None):
        self.data = data
        self.reader = reader
        self.remote = bytearray()
        self.state = "open"
        self.revision = 1
        self.calls = []
        self.fail_begin_once = False
        self.fail_append_once = False
        self.fail_attestation = None
        self.pause_attestation = None
        self.release_attestation = None
        self.return_verifying = False
        self.envelope_attestation = None
        self.stale_inspect_after_append = False
        self.attestation = recipient_mobility_attestation(
            {
                "protocol_version": "mobility-v1",
                "receiver_identity_id": "receiver-a",
                "principal_id": "principal-a",
                "project_id": "project-a",
                "authorized_source_owner_id": "source-owner-a",
                "grant_id": "grant-a",
                "grant_revision": 1,
                "can_write": True,
                "max_object_bytes": len(data),
            }
        )

    @property
    def spec(self):
        return {
            "sha256": hashlib.sha256(self.data).hexdigest(),
            "size_bytes": len(self.data),
            "media_type": "application/octet-stream",
        }

    def _receipt(self):
        receipt = {
            "transfer_id": _TRANSFER_ID,
            "state": self.state,
            "offset": len(self.remote),
            "chunk_bytes": 65536,
            "expires_at": 1060.0,
            "revision": self.revision,
        }
        if self.state == "sealed":
            receipt["artifact"] = {"artifact_id": _ARTIFACT_ID, **self.spec}
        return receipt

    def _envelope(self, command_id):
        return {
            "protocol_version": "mobility-v1",
            "recipient_attestation": self.envelope_attestation or self.attestation,
            "command_id": command_id,
            "spec": self.spec,
            "receipt": self._receipt(),
        }

    def recipient_attestation(self, spec):
        self.calls.append(("attestation", dict(spec)))
        if self.pause_attestation is not None:
            self.pause_attestation.set()
            assert self.release_attestation.wait(10)
        if self.fail_attestation:
            raise TransferError(self.fail_attestation)
        return dict(self.attestation)

    def begin(self, spec, command_id, receipt_capability):
        self.calls.append(("begin", command_id, receipt_capability))
        assert spec == self.spec
        if self.fail_begin_once:
            self.fail_begin_once = False
            raise TransferError("MOBILITY_UNAVAILABLE")
        return self._envelope(command_id)

    def inspect_receipt(self, transfer_id, command_id, spec, receipt_capability):
        self.calls.append(("inspect", transfer_id, command_id, receipt_capability))
        assert transfer_id == _TRANSFER_ID
        assert spec == self.spec
        if self.stale_inspect_after_append and self.remote:
            self.stale_inspect_after_append = False
            envelope = self._envelope(command_id)
            envelope["receipt"] = {
                **envelope["receipt"],
                "offset": 0,
                "revision": 1,
            }
            return envelope
        return self._envelope(command_id)

    def append(self, envelope, immutable_spec, body, receipt_capability):
        offset = envelope["receipt"]["offset"]
        self.calls.append(("append", offset, bytes(body), receipt_capability))
        assert immutable_spec == self.spec
        assert offset == len(self.remote)
        self.remote.extend(body)
        self.revision += 1
        if self.reader is not None and len(self.remote) == 65536:
            self.reader.revoked = True
        if self.fail_append_once:
            self.fail_append_once = False
            raise TransferError("MOBILITY_UNAVAILABLE")
        return {
            "offset": offset,
            "next_offset": len(self.remote),
            "chunk_sha256": hashlib.sha256(body).hexdigest(),
            "revision": self.revision,
        }

    def seal(self, envelope, immutable_spec, seal_command_id, receipt_capability):
        self.calls.append(("seal", seal_command_id, receipt_capability))
        assert immutable_spec == self.spec
        assert bytes(self.remote) == self.data
        self.state = "verifying" if self.return_verifying else "sealed"
        self.revision += 1
        return self._envelope(envelope["command_id"])


def _services(tmp_path, data, *, peer=None, reader=None, clock=None, context=None):
    reader = reader or _Reader(data)
    peer = peer or _Peer(data, reader=reader)
    clock = clock or _Clock()
    context = context or _ContextProvider(_context())
    repository = SQLiteArtifactMobilityJournal(tmp_path / "private-journal")
    journal = mobility.ArtifactMobilityJournal(
        repository,
        clock=clock,
        operation_id_factory=lambda: "a" * 32,
        receipt_capability_factory=lambda: _CAPABILITY,
    )
    service_type = getattr(mobility, "ArtifactMobilityDispatchService")
    service = service_type(
        source_reader=reader,
        peer=peer,
        repository=repository,
        journal=journal,
        current_context=context,
        clock=clock,
    )
    return repository, service, peer, reader, clock, context


def test_lost_begin_response_resumes_with_one_canonical_command(tmp_path):
    data = b"stable-command"
    peer = _Peer(data)
    peer.fail_begin_once = True
    repository, service, peer, _reader, _clock, _context_provider = _services(
        tmp_path, data, peer=peer
    )

    interrupted = service.send(_SOURCE_ID, confirm_destination="node-one")
    assert interrupted.state == "resumable"
    resumed = service.resume(interrupted.operation_id)

    assert resumed.state == "sealed"
    begin_commands = [call[1] for call in peer.calls if call[0] == "begin"]
    assert begin_commands == [
        "mobility-v1." + "4" * 16 + "." + "a" * 32,
        "mobility-v1." + "4" * 16 + "." + "a" * 32,
    ]
    assert (
        repository.public_status(resumed.operation_id, "source-owner-a")["state"]
        == "sealed"
    )


def test_resume_rejects_changed_receipt_envelope_before_post_restart_append(tmp_path):
    data = b"x" * 70000
    peer = _Peer(data)
    peer.fail_append_once = True
    repository, service, peer, _reader, _clock, context = _services(
        tmp_path, data, peer=peer
    )
    interrupted = service.send(_SOURCE_ID, confirm_destination="node-one")
    assert interrupted.state == "resumable"
    assert len(peer.remote) == 65536

    peer.envelope_attestation = {**peer.attestation, "grant_revision": 2}
    reopened_journal = mobility.ArtifactMobilityJournal(repository, clock=_Clock())
    service_type = getattr(mobility, "ArtifactMobilityDispatchService")
    reopened = service_type(
        source_reader=_Reader(data),
        peer=peer,
        repository=repository,
        journal=reopened_journal,
        current_context=context,
        clock=_Clock(),
    )
    before = len([call for call in peer.calls if call[0] == "append"])
    blocked = reopened.resume(interrupted.operation_id)

    assert blocked.state == "terminal_blocked"
    assert blocked.outcome_code == "MOBILITY_INTEGRITY"
    assert len([call for call in peer.calls if call[0] == "append"]) == before


def test_changed_credential_binding_terminally_blocks_before_peer_call(tmp_path):
    data = b"binding-fence"
    peer = _Peer(data)
    peer.fail_begin_once = True
    repository, service, peer, _reader, _clock, context = _services(
        tmp_path, data, peer=peer
    )
    interrupted = service.send(_SOURCE_ID, confirm_destination="node-one")
    peer.calls.clear()
    context.value = _context(
        credential_generation="7" * 64,
        destination_binding_hmac="8" * 64,
        credential_material="rotated-" + "k" * 32,
    )

    blocked = service.resume(interrupted.operation_id)

    assert blocked.state == "terminal_blocked"
    assert blocked.outcome_code == "IMMUTABLE_FENCE"
    assert peer.calls == []
    assert repository.tombstone_exists(
        blocked.source_owner_id, blocked.destination_scope_id, blocked.operation_id
    )


def test_changed_remote_identity_terminally_blocks_before_append(tmp_path):
    data = b"remote-identity"
    peer = _Peer(data)
    peer.fail_begin_once = True
    repository, service, peer, _reader, _clock, _context_provider = _services(
        tmp_path, data, peer=peer
    )
    interrupted = service.send(_SOURCE_ID, confirm_destination="node-one")
    peer.calls.clear()
    peer.fail_attestation = "MOBILITY_ATTESTATION"

    blocked = service.resume(interrupted.operation_id)

    assert blocked.state == "terminal_blocked"
    assert blocked.outcome_code == "MOBILITY_INTEGRITY"
    assert [call[0] for call in peer.calls] == ["attestation"]


def test_source_revocation_between_chunks_stops_before_another_peer_call(tmp_path):
    data = b"z" * 70000
    reader = _Reader(data)
    peer = _Peer(data, reader=reader)
    _repository, service, peer, _reader, _clock, _context_provider = _services(
        tmp_path, data, peer=peer, reader=reader
    )

    blocked = service.send(_SOURCE_ID, confirm_destination="node-one")

    assert blocked.state == "terminal_blocked"
    assert blocked.outcome_code == "IMMUTABLE_FENCE"
    assert [call[0] for call in peer.calls].count("append") == 1
    assert len(peer.remote) == 65536


def test_append_ack_requires_exact_durable_receipt_before_next_chunk(tmp_path):
    data = b"r" * 70000
    peer = _Peer(data)
    peer.stale_inspect_after_append = True
    repository, service, peer, _reader, _clock, _context_provider = _services(
        tmp_path, data, peer=peer
    )

    blocked = service.send(_SOURCE_ID, confirm_destination="node-one")

    assert blocked.state == "terminal_blocked"
    assert blocked.outcome_code == "MOBILITY_INTEGRITY"
    assert [call[0] for call in peer.calls].count("append") == 1
    assert len(peer.remote) == 65536
    durable = repository.load_operation(blocked.operation_id, blocked.source_owner_id)
    assert durable.receipt.offset == 0
    assert durable.receipt.revision == 1


def test_expired_paused_sender_keeps_os_lock_and_cannot_make_next_peer_call(tmp_path):
    data = b"paused"
    clock = _Clock()
    pause, release = threading.Event(), threading.Event()
    peer = _Peer(data)
    peer.pause_attestation = pause
    peer.release_attestation = release
    repository, service, peer, _reader, _clock, _context_provider = _services(
        tmp_path, data, peer=peer, clock=clock
    )
    result = []

    def run_sender():
        try:
            result.append(service.send(_SOURCE_ID, confirm_destination="node-one"))
        except mobility.MobilityJournalError as error:
            result.append(str(error))

    sender = threading.Thread(target=run_sender)
    sender.start()
    assert pause.wait(10)
    clock.value = 1002.0
    assert repository.recover_expired_leases(now=clock.value) == ("a" * 32,)
    with pytest.raises(mobility.MobilityJournalError, match="BUSY"):
        service.resume("a" * 32)
    release.set()
    sender.join(10)

    assert not sender.is_alive()
    assert result == ["LEASE_LOST"]
    assert [call[0] for call in peer.calls] == ["attestation"]

    replacement_peer = _Peer(data)
    replacement_type = getattr(mobility, "ArtifactMobilityDispatchService")
    replacement = replacement_type(
        source_reader=_Reader(data),
        peer=replacement_peer,
        repository=repository,
        journal=mobility.ArtifactMobilityJournal(repository, clock=clock),
        current_context=_ContextProvider(_context()),
        clock=clock,
    )
    assert replacement.resume("a" * 32).state == "sealed"
    assert [call[0] for call in replacement_peer.calls].count("begin") == 1


def test_verifying_return_stops_synchronously_until_explicit_resume(tmp_path):
    data = b"explicit-only"
    peer = _Peer(data)
    peer.return_verifying = True
    _repository, service, peer, _reader, _clock, _context_provider = _services(
        tmp_path, data, peer=peer
    )
    before_threads = {thread.ident for thread in threading.enumerate()}

    waiting = service.send(_SOURCE_ID, confirm_destination="node-one")
    calls_after_return = tuple(peer.calls)

    assert waiting.state == "awaiting_seal"
    assert tuple(peer.calls) == calls_after_return
    assert {thread.ident for thread in threading.enumerate()} == before_threads
    assert [call[0] for call in peer.calls].count("seal") == 1
    assert [call[0] for call in peer.calls].count("inspect") == 1
