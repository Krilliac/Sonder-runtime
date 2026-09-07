"""Receiver-only mobility-v1 protocol regressions."""

import hashlib
import sqlite3
import time
from dataclasses import replace

import pytest

from sonder_runtime.application.artifacts.transfer import TransferError
from sonder_runtime.bootstrap.artifact_transfer import ArtifactTransferBinding
from sonder_runtime.platform.artifact_transfer_config import ArtifactTransferConfig
from sonder_runtime.platform.config import Secrets, SonderConfig, StateConfig


def _receiver(tmp_path, *, can_read=True, receiver_identity_id="receiver-a"):
    config = SonderConfig(
        state=StateConfig(home=str(tmp_path / "home")),
        secrets=Secrets(
            api_key="administrator-" + "a" * 32,
            artifact_transfer_key="transfer-" + "b" * 32,
        ),
        artifact_transfer=ArtifactTransferConfig(
            enabled=True,
            store_dir=str(tmp_path / "receiver-private"),
            principal_id="alice",
            project_id="project-a",
            peer_node_id="source-owner-a",
            receiver_identity_id=receiver_identity_id,
            grant_id="grant-a",
            grant_revision=1,
            expires_at=int(time.time()) + 3600,
            can_read=can_read,
            can_write=True,
        ),
    )
    binding = ArtifactTransferBinding(lambda: config)
    context = binding.authenticate(
        "Bearer " + config.secrets.artifact_transfer_key, correlation_id="mobility-test"
    )
    return binding, context


def _spec(data=b"mobility protocol payload"):
    return {
        "sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
        "media_type": "application/octet-stream",
    }


def _begin(binding, context, *, command="mobility-begin", capability="a" * 64):
    return binding.service().begin_mobility_upload(
        _spec(), command, binding.mobility_contract(context, capability), context
    )


def test_mobility_replay_keeps_the_original_verifier_and_receipt(tmp_path):
    binding, context = _receiver(tmp_path)
    capability = "a" * 64
    try:
        first = _begin(binding, context, capability=capability)
        transfer_id = first["receipt"]["transfer_id"]
        database = binding.service().store.root / "transfers.sqlite"
        with sqlite3.connect(database) as conn:
            before = conn.execute(
                "SELECT command,spec,mobility_verifier FROM artifact_uploads WHERE id=?",
                (transfer_id,),
            ).fetchone()
        assert before is not None
        assert capability not in before
        assert capability not in repr(first)
        assert _begin(binding, context, capability=capability) == first
        with pytest.raises(TransferError, match="FORBIDDEN"):
            _begin(binding, context, capability="c" * 64)
        with sqlite3.connect(database) as conn:
            after = conn.execute(
                "SELECT command,spec,mobility_verifier FROM artifact_uploads WHERE id=?",
                (transfer_id,),
            ).fetchone()
        assert after == before
    finally:
        binding.close()


def test_write_only_mobility_receipt_requires_exact_transfer_and_command(tmp_path):
    binding, context = _receiver(tmp_path, can_read=False)
    capability = "a" * 64
    try:
        envelope = _begin(binding, context, capability=capability)
        service = binding.service()
        transfer_id = envelope["receipt"]["transfer_id"]
        contract = binding.mobility_contract(context, capability)
        assert service.inspect_mobility_upload(
            transfer_id, "mobility-begin", contract, context
        ) == envelope
        with pytest.raises(TransferError, match="FORBIDDEN"):
            service.inspect_mobility_upload(
                "f" * 32, "mobility-begin", contract, context
            )
        with pytest.raises(TransferError, match="FORBIDDEN"):
            service.inspect_mobility_upload(
                transfer_id, "wrong-command", contract, context
            )
        with pytest.raises(TransferError, match="FORBIDDEN"):
            service.inspect_upload(transfer_id, context)
        with pytest.raises(TransferError, match="FORBIDDEN"):
            service.inspect_artifact(transfer_id, context)
        with pytest.raises(TransferError, match="FORBIDDEN"):
            service.read_range(transfer_id, 0, 1, context)
    finally:
        binding.close()


def test_mobility_mutations_hide_unknown_transfer_ids(tmp_path):
    binding, context = _receiver(tmp_path)
    capability = "a" * 64
    unknown = "f" * 32
    data = b"mobility protocol payload"
    try:
        service = binding.service()
        contract = binding.mobility_contract(context, capability)
        with pytest.raises(TransferError, match="FORBIDDEN"):
            service.append_mobility_chunk(
                unknown,
                0,
                hashlib.sha256(data).hexdigest(),
                data,
                contract,
                context,
            )
        with pytest.raises(TransferError, match="FORBIDDEN"):
            service.abort_mobility_upload(unknown, "mobility-abort", contract, context)
    finally:
        binding.close()


def test_legacy_and_mobility_rows_cannot_be_downgraded_or_retrofitted(tmp_path):
    binding, context = _receiver(tmp_path)
    try:
        envelope = _begin(binding, context)
        service = binding.service()
        transfer_id = envelope["receipt"]["transfer_id"]
        with pytest.raises(TransferError, match="MOBILITY_PROTOCOL"):
            service.begin_upload(_spec(), "mobility-begin", context)
        with pytest.raises(TransferError, match="MOBILITY_PROTOCOL"):
            service.inspect_upload(transfer_id, context)
        with pytest.raises(TransferError, match="MOBILITY_PROTOCOL"):
            service.append_chunk(
                transfer_id, 0, _spec()["sha256"], b"mobility protocol payload", context
            )
        legacy = service.begin_upload(_spec(), "legacy-begin", context)
        contract = binding.mobility_contract(context, "a" * 64)
        with pytest.raises(TransferError, match="MOBILITY_PROTOCOL"):
            service.begin_mobility_upload(
                _spec(),
                "legacy-begin",
                contract,
                context,
            )
        with pytest.raises(TransferError, match="MOBILITY_PROTOCOL"):
            service.inspect_mobility_upload(
                legacy["transfer_id"], "legacy-begin", contract, context
            )
        assert legacy["transfer_id"]
    finally:
        binding.close()


def test_missing_receiver_identity_rejects_mobility_before_legacy_dedup(tmp_path):
    binding, context = _receiver(tmp_path, receiver_identity_id="")
    try:
        with pytest.raises(PermissionError):
            binding.mobility_contract(context, "a" * 64)
        # The failed mobility admission did not create a command row. Legacy is unchanged.
        receipt = binding.service().begin_upload(_spec(), "mobility-begin", context)
        assert receipt["transfer_id"]
    finally:
        binding.close()


def test_rotated_receiver_bearer_cannot_replay_the_original_capability(tmp_path):
    binding, context = _receiver(tmp_path)
    capability = "a" * 64
    try:
        _begin(binding, context, capability=capability)
        current = [binding.current_config()]
        binding._config_provider = lambda: current[0]
        current[0] = replace(
            current[0],
            secrets=replace(
                current[0].secrets, artifact_transfer_key="rotated-transfer-" + "c" * 32
            ),
        )
        rotated_context = binding.authenticate(
            "Bearer " + current[0].secrets.artifact_transfer_key,
            correlation_id="mobility-rotated",
        )
        with pytest.raises(TransferError, match="FORBIDDEN"):
            binding.service().begin_mobility_upload(
                _spec(),
                "mobility-begin",
                binding.mobility_contract(rotated_context, capability),
                rotated_context,
            )
    finally:
        binding.close()
