"""Typed backup seam, compatibility identity, and caller routing."""
from __future__ import annotations

import importlib
import inspect
import sys
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
