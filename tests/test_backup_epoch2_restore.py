"""A standard backup of an adopted epoch-2 home must restore to a servable home.

``create_backup`` used to copy only the migrations-registry stores, so the
epoch-2 domain databases that ``serve`` requires (automation.db, selfmod.db,
training.db) and the adoption receipt were never captured.  A home restored
by the runbook then passed ``restore smoke`` and refused to start.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from sonder_runtime.__main__ import main
from sonder_runtime.adapters import backup as backup_adapter
from sonder_runtime.adapters.persistence.sqlite.bridge_migration import (
    EPOCH2_DATABASES,
    require_epoch_2,
)


@pytest.fixture
def adopted_home(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SONDER_HOME", str(home))
    for name in ("SONDER_CONFIG", "SONDER_SECRETS", "SONDER_DB", "SONDER_FLEET_DB"):
        monkeypatch.delenv(name, raising=False)
    assert main(["migrate", "--json"]) == 0
    assert main(["migrate", "--adopt-epoch2", "--json"]) == 0
    capsys.readouterr()
    require_epoch_2(home)  # precondition: the live home can serve
    return home


def test_backup_captures_every_epoch2_database_and_receipt(adopted_home, tmp_path):
    result = backup_adapter.create_backup(tmp_path / "backups")

    state = {p.name for p in (result.path / "state").iterdir()}
    for name in EPOCH2_DATABASES:
        assert name in state, name
    assert "epoch2_adoption_receipt.json" in state
    assert backup_adapter.verify_backup(result.path) == []


def test_restored_home_passes_the_serve_epoch_gate(adopted_home, tmp_path):
    result = backup_adapter.create_backup(tmp_path / "backups")
    restored = tmp_path / "restored"

    backup_adapter.restore_to_empty(result.path, restored)

    require_epoch_2(restored)
    assert backup_adapter.restore_smoke(result.path) == []


def test_restore_smoke_fails_when_serve_would_refuse_the_home(
    adopted_home, tmp_path,
):
    result = backup_adapter.create_backup(tmp_path / "backups")
    # Simulate a backup taken by an older build: drop an epoch-2 domain DB
    # from the manifest, the state directory and the checksum index.
    manifest_path = result.path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"] = [
        f for f in manifest["files"]
        if Path(f["path"]) != Path("state", "automation.db")
    ]
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (result.path / "state" / "automation.db").unlink()
    import hashlib

    lines = "".join(
        f"{f['sha256']}  {f['path']}\n" for f in manifest["files"]
    ) + f"{hashlib.sha256(manifest_path.read_bytes()).hexdigest()}  manifest.json\n"
    (result.path / "checksums.sha256").write_text(lines, encoding="utf-8")
    assert backup_adapter.verify_backup(result.path) == []

    problems = backup_adapter.restore_smoke(result.path)

    assert any("epoch" in p and "automation.db" in p for p in problems), problems


def test_backup_rejects_a_non_object_adoption_receipt(adopted_home, tmp_path):
    (adopted_home / "epoch2_adoption_receipt.json").write_text("[]", encoding="utf-8")

    with pytest.raises(backup_adapter.BackupError, match="receipt"):
        backup_adapter.create_backup(tmp_path / "backups")


def test_epoch2_copies_are_consistent_sqlite_images(adopted_home, tmp_path):
    result = backup_adapter.create_backup(tmp_path / "backups")
    conn = sqlite3.connect(str(result.path / "state" / "automation.db"))
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        conn.close()
