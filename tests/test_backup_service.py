"""Typed backup seam, compatibility identity, and caller routing."""
from __future__ import annotations

import importlib
import hashlib
import inspect
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import sonder_backup
import sonder_update_engine
from sonder_runtime.adapters import backup as backup_adapter
from sonder_runtime.adapters.legacy.backup import LegacyBackupGateway
from sonder_runtime.application.backup import BackupService


class CapturingGateway:
    def __init__(self):
        self.calls = []
        self.result = SimpleNamespace(
            backup_id="id", path="path", total_bytes=9, file_count=2
        )

    def create(self, target):
        self.calls.append(("create", target, {}))
        return self.result

    def verify(self, backup_dir):
        self.calls.append(("verify", backup_dir, {}))
        return ["problem"]

    def list(self, target):
        self.calls.append(("list", target, {}))
        return [{"path": "one"}]

    def prune(self, target, **options):
        self.calls.append(("prune", target, options))
        return ["old"]

    def prune_tiered(self, target, **options):
        self.calls.append(("prune_tiered", target, options))
        return ["older"]

    def smoke_restore(self, backup_dir):
        self.calls.append(("smoke_restore", backup_dir, {}))
        return ["unsafe"]

    def restore_to_empty(self, backup_dir, destination):
        self.calls.append(
            ("restore_to_empty", backup_dir, {"destination": destination})
        )
        return ["restored"]


def test_service_forwards_every_operation_and_result_without_reformatting():
    gateway = CapturingGateway()
    service = BackupService(gateway)

    assert service.create("target") is gateway.result
    assert service.verify("backup") == ["problem"]
    assert service.list("target") == [{"path": "one"}]
    assert service.prune("target", keep=0) == ["old"]
    assert service.prune_tiered(
        "target", daily=1, weekly=2, monthly=3
    ) == ["older"]
    assert service.smoke_restore("backup") == ["unsafe"]
    assert service.restore_to_empty("backup", "destination") == ["restored"]
    assert gateway.calls == [
        ("create", "target", {}),
        ("verify", "backup", {}),
        ("list", "target", {}),
        ("prune", "target", {"keep": 0}),
        (
            "prune_tiered",
            "target",
            {"daily": 1, "weekly": 2, "monthly": 3},
        ),
        ("smoke_restore", "backup", {}),
        ("restore_to_empty", "backup", {"destination": "destination"}),
    ]


def test_service_does_not_translate_gateway_errors():
    expected = backup_adapter.BackupError("private path must remain adapter-owned")
    gateway = CapturingGateway()
    gateway.verify = lambda _path: (_ for _ in ()).throw(expected)

    with pytest.raises(backup_adapter.BackupError) as caught:
        BackupService(gateway).verify("opaque")
    assert caught.value is expected


def test_root_backup_is_true_reload_safe_compatibility_alias():
    assert sonder_backup is backup_adapter
    original = sonder_backup.MANIFEST_FORMAT_VERSION
    sonder_backup.MANIFEST_FORMAT_VERSION = 99
    assert backup_adapter.MANIFEST_FORMAT_VERSION == 99
    sonder_backup.MANIFEST_FORMAT_VERSION = original
    assert importlib.reload(sonder_backup) is backup_adapter
    assert sys.modules["sonder_backup"] is sys.modules[
        "sonder_runtime.adapters.backup"
    ]


def test_legacy_gateway_resolves_live_adapter_module(monkeypatch):
    calls = []
    replacement = SimpleNamespace(
        verify_backup=lambda path: calls.append(path) or ["live"]
    )
    monkeypatch.setitem(sys.modules, "sonder_runtime.adapters.backup", replacement)

    assert LegacyBackupGateway().verify("backup") == ["live"]
    assert calls == ["backup"]


def test_update_engine_routes_backup_through_application_service():
    source = inspect.getsource(sonder_update_engine.UpdateManager.install)
    assert "default_app().backup.create(target)" in source
    assert "sonder_backup.create_backup" not in source


def _write_fixture_backup(root, *, rel="state/memory.db", content=b"safe"):
    member = root / rel
    member.parent.mkdir(parents=True, exist_ok=True)
    member.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    manifest = {
        "format_version": backup_adapter.MANIFEST_FORMAT_VERSION,
        "files": [{"path": rel, "size": len(content), "sha256": digest}],
    }
    manifest_path = root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    manifest_digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    (root / "checksums.sha256").write_text(
        f"{digest}  {rel}\n{manifest_digest}  manifest.json\n",
        encoding="utf-8",
    )
    return member


def test_verify_rejects_symlinked_member_and_restore_destination(tmp_path):
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks unavailable")
    backup = tmp_path / "backup"
    outside = tmp_path / "private.db"
    outside.write_bytes(b"safe")
    member = _write_fixture_backup(backup)
    member.unlink()
    try:
        member.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation unavailable")

    assert any(
        "regular file" in problem
        for problem in backup_adapter.verify_backup(backup)
    )

    member.unlink()
    member.write_bytes(b"safe")
    real_destination = tmp_path / "real-destination"
    real_destination.mkdir()
    destination_link = tmp_path / "destination-link"
    destination_link.symlink_to(real_destination, target_is_directory=True)
    with pytest.raises(backup_adapter.BackupError, match="must not be a symlink"):
        backup_adapter.restore_to_empty(backup, destination_link)
    assert list(real_destination.iterdir()) == []


def test_verify_bounds_and_validates_untrusted_manifest(tmp_path):
    backup = tmp_path / "backup"
    _write_fixture_backup(backup)
    (backup / "manifest.json").write_bytes(
        b"{" + b" " * backup_adapter.MAX_MANIFEST_BYTES + b"}"
    )
    assert backup_adapter.verify_backup(backup) == [
        "manifest.json exceeds the size limit"
    ]

    _write_fixture_backup(backup)
    manifest_path = backup / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][0]["size"] = True
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert any(
        "invalid metadata" in problem
        for problem in backup_adapter.verify_backup(backup)
    )


def test_verify_rejects_checksum_index_tampering_and_flattening_collision(tmp_path):
    backup = tmp_path / "backup"
    _write_fixture_backup(backup)
    (backup / "checksums.sha256").write_text("forged\n", encoding="utf-8")
    assert "checksums.sha256 does not match manifest" in (
        backup_adapter.verify_backup(backup)
    )

    _write_fixture_backup(backup)
    manifest_path = backup / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"].append(
        {**manifest["files"][0], "path": "state/nested/memory.db"}
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert any(
        "escapes backup" in problem
        for problem in backup_adapter.verify_backup(backup)
    )


def test_restore_is_published_only_after_every_copy_verifies(tmp_path, monkeypatch):
    backup = tmp_path / "backup"
    _write_fixture_backup(backup)
    destination = tmp_path / "restored"
    real_hash = backup_adapter._sha256_file

    def corrupt_staged_copy(path):
        if ".restore-" in str(path):
            return "0" * 64
        return real_hash(path)

    monkeypatch.setattr(backup_adapter, "_sha256_file", corrupt_staged_copy)
    with pytest.raises(backup_adapter.BackupError, match="restored file corrupt"):
        backup_adapter.restore_to_empty(backup, destination)
    assert not destination.exists()
    assert not list(tmp_path.glob(".restored.restore-*"))


def test_restore_relative_dot_stages_beside_destination(tmp_path, monkeypatch):
    backup = tmp_path / "backup"
    _write_fixture_backup(backup)
    destination = tmp_path / "empty-destination"
    destination.mkdir()
    monkeypatch.chdir(destination)
    def assert_sibling_staging(*, prefix, dir):
        assert Path(dir) == tmp_path
        raise OSError("stop after staging-location assertion")

    monkeypatch.setattr(tempfile, "mkdtemp", assert_sibling_staging)
    with pytest.raises(OSError, match="staging-location assertion"):
        backup_adapter.restore_to_empty(backup, ".")


@pytest.mark.parametrize("value", [True, 1.5, "2", -1, 0])
def test_retention_rejects_non_integer_and_non_positive_keep(tmp_path, value):
    with pytest.raises(backup_adapter.BackupError):
        backup_adapter.prune_backups(tmp_path, keep=value)
