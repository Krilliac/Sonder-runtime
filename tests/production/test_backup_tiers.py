"""SPEC-2 WP7: tiered retention and restore smoke."""
from __future__ import annotations

import json
import sqlite3

import pytest

import sonder_backup

pytestmark = pytest.mark.unit


@pytest.fixture()
def isolated_state(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("SONDER_DB", str(state / "memory.db"))
    monkeypatch.setenv("SONDER_AUTOPILOT_DB", str(state / "autopilot.db"))
    monkeypatch.setenv("SONDER_FLEET_DB", str(state / "fleet.db"))
    monkeypatch.setenv("SONDER_OPERATIONS_DB", str(state / "operations.db"))
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(state / "runtime_policy.json"))
    conn = sqlite3.connect(str(state / "memory.db"))
    conn.execute("CREATE TABLE facts (id INTEGER PRIMARY KEY, body TEXT)")
    conn.execute("INSERT INTO facts (body) VALUES ('alpha')")
    conn.commit()
    conn.close()
    return tmp_path


def _redate(backup_path, stamp):
    manifest_path = backup_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["created_at_utc"] = stamp
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    # Keep the backup verifiable: manifest.json itself is not in `files`,
    # so its rewrite does not break per-file verification.


def test_tiered_prune_keeps_daily_weekly_monthly(isolated_state):
    target = isolated_state / "backups"
    stamps = [
        "2026-08-04T10:00:00.000000Z",  # today
        "2026-08-03T10:00:00.000000Z",  # yesterday
        "2026-07-28T10:00:00.000000Z",  # last week
        "2026-06-15T10:00:00.000000Z",  # june
        "2026-06-14T10:00:00.000000Z",  # june, older same-month
        "2026-01-10T10:00:00.000000Z",  # far past
    ]
    paths = []
    for stamp in stamps:
        result = sonder_backup.create_backup(target)
        _redate(result.path, stamp)
        paths.append(result.path)

    removed = sonder_backup.prune_backups_tiered(
        target, daily=2, weekly=2, monthly=3
    )
    remaining = {e["created_at_utc"] for e in sonder_backup.list_backups(target)}
    # Two daily (today, yesterday), the last-week newest, the june newest.
    assert "2026-08-04T10:00:00.000000Z" in remaining
    assert "2026-08-03T10:00:00.000000Z" in remaining
    assert "2026-07-28T10:00:00.000000Z" in remaining
    assert "2026-06-15T10:00:00.000000Z" in remaining
    # Older same-month june copy and the far-past backup go.
    assert "2026-06-14T10:00:00.000000Z" not in remaining
    assert "2026-01-10T10:00:00.000000Z" not in remaining
    assert len(removed) == 2


def test_tiered_prune_never_removes_only_backup(isolated_state):
    target = isolated_state / "backups"
    result = sonder_backup.create_backup(target)
    removed = sonder_backup.prune_backups_tiered(
        target, daily=1, weekly=1, monthly=1
    )
    assert removed == []
    assert sonder_backup.verify_backup(result.path) == []


def test_restore_smoke_passes_on_good_backup(isolated_state):
    target = isolated_state / "backups"
    result = sonder_backup.create_backup(target)
    assert sonder_backup.restore_smoke(result.path) == []


def test_restore_smoke_fails_on_corrupt_db(isolated_state):
    target = isolated_state / "backups"
    result = sonder_backup.create_backup(target)
    victim = result.path / "state" / "memory.db"
    data = bytearray(victim.read_bytes())
    # Corrupt a page in the middle, keeping length (size check passes,
    # integrity/hash must catch it).
    if len(data) > 2048:
        data[1500:1600] = b"\xff" * 100
    victim.write_bytes(bytes(data))
    problems = sonder_backup.restore_smoke(result.path)
    assert problems  # hash mismatch at minimum
