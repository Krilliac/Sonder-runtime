"""SPEC-2 WP7: tiered retention and restore smoke."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from sonder_runtime.adapters import backup as sonder_backup

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
    _reseal(backup_path)


def _reseal(backup_path):
    """Re-bind checksums.sha256 to a rewritten manifest so it still verifies."""
    checksums = backup_path / "checksums.sha256"
    lines = checksums.read_text(encoding="utf-8").splitlines(keepends=True)
    digest = hashlib.sha256(
        (backup_path / "manifest.json").read_bytes()
    ).hexdigest()
    lines = [
        line for line in lines if not line.endswith("  manifest.json\n")
    ] + [f"{digest}  manifest.json\n"]
    checksums.write_text("".join(lines), encoding="utf-8")


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


def _strip_created_at(backup_path):
    manifest_path = backup_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["created_at_utc"]
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    _reseal(backup_path)


def _dated_undated_and_garbled(target):
    good = sonder_backup.create_backup(target).path
    _redate(good, "2026-09-26T00:00:00.000000Z")
    undated = sonder_backup.create_backup(target).path
    _strip_created_at(undated)
    garbled = sonder_backup.create_backup(target).path
    _redate(garbled, "not-a-timestamp")
    return good, undated, garbled


def test_list_ranks_undated_manifests_after_every_dated_backup(isolated_state):
    target = isolated_state / "backups"
    good, undated, garbled = _dated_undated_and_garbled(target)

    entries = sonder_backup.list_backups(target)

    assert entries[0]["path"] == str(good)
    assert entries[0]["created_at_valid"] is True
    assert {e["path"] for e in entries[1:]} == {str(undated), str(garbled)}
    assert all(e["created_at_valid"] is False for e in entries[1:])
    by_path = {e["path"]: e for e in entries}
    assert by_path[str(undated)]["created_at_utc"] == "unknown"
    assert by_path[str(garbled)]["created_at_utc"] == "not-a-timestamp"


def test_list_orders_mixed_precision_stamps_chronologically(isolated_state):
    target = isolated_state / "backups"
    whole_second = sonder_backup.create_backup(target).path
    _redate(whole_second, "2026-09-26T00:00:00Z")
    later_fraction = sonder_backup.create_backup(target).path
    _redate(later_fraction, "2026-09-26T00:00:00.500000Z")
    earlier_day = sonder_backup.create_backup(target).path
    _redate(earlier_day, "2026-09-25T23:59:59.999999Z")

    assert [e["path"] for e in sonder_backup.list_backups(target)] == [
        str(later_fraction),
        str(whole_second),
        str(earlier_day),
    ]


def test_keep_n_prune_never_spends_a_slot_on_an_undated_backup(isolated_state):
    target = isolated_state / "backups"
    good, undated, garbled = _dated_undated_and_garbled(target)

    removed = sonder_backup.prune_backups(target, keep=1)

    assert sorted(removed) == sorted([str(undated), str(garbled)])
    assert [e["path"] for e in sonder_backup.list_backups(target)] == [str(good)]


def test_tiered_prune_keeps_dated_backups_over_undated_ones(isolated_state):
    target = isolated_state / "backups"
    good, undated, garbled = _dated_undated_and_garbled(target)

    removed = sonder_backup.prune_backups_tiered(
        target, daily=1, weekly=1, monthly=1
    )

    assert sorted(removed) == sorted([str(undated), str(garbled)])
    assert good.is_dir()


def test_prune_keeps_undated_backup_when_it_is_the_only_verified_one(
    isolated_state,
):
    target = isolated_state / "backups"
    undated = sonder_backup.create_backup(target).path
    _strip_created_at(undated)
    broken = sonder_backup.create_backup(target).path
    _redate(broken, "2026-09-26T00:00:00.000000Z")
    (broken / "state" / "memory.db").write_bytes(b"corrupt")

    assert sonder_backup.prune_backups_tiered(
        target, daily=1, weekly=1, monthly=1
    ) == []
    assert sonder_backup.prune_backups(target, keep=1) == []
    assert undated.is_dir()


def _restore_smoke_unit_selector():
    unit = (
        Path(__file__).resolve().parents[2]
        / "packaging" / "systemd" / "sonder-restore-smoke.service"
    ).read_text(encoding="utf-8")
    match = re.search(r'python -c "(.*?)"\); \\', unit)
    assert match, "restore-smoke unit no longer selects via python -c"
    return match.group(1).replace('\\"', '"')


def _run_unit_selector(target):
    listing = json.dumps({"backups": sonder_backup.list_backups(target)})
    return subprocess.run(
        [sys.executable, "-c", _restore_smoke_unit_selector()],
        input=listing, capture_output=True, text=True, check=True,
    ).stdout.strip()


def test_restore_smoke_unit_selects_the_newest_dated_backup(isolated_state):
    target = isolated_state / "backups"
    good, _undated, _garbled = _dated_undated_and_garbled(target)

    selected = _run_unit_selector(target)

    assert selected == str(good)
    assert sonder_backup.restore_smoke(selected) == []


def test_restore_smoke_unit_selects_nothing_without_a_dated_backup(
    isolated_state,
):
    target = isolated_state / "backups"
    undated = sonder_backup.create_backup(target).path
    _strip_created_at(undated)

    # The unit's `test -n "$latest"` then fails the run loudly.
    assert _run_unit_selector(target) == ""


def _raw_pre_epoch2(target, stamp):
    """A manifest-less ``migrate --adopt-epoch2`` safety copy."""
    path = target / f"pre-epoch2-{stamp}"
    path.mkdir(parents=True)
    (path / "memory.db").write_bytes(b"")
    return path


def test_undated_standard_backup_ranks_after_pre_epoch2_copies(isolated_state):
    target = isolated_state / "backups"
    good = sonder_backup.create_backup(target).path
    _redate(good, "2026-09-26T00:00:00.000000Z")
    undated = sonder_backup.create_backup(target).path
    _strip_created_at(undated)
    raw = _raw_pre_epoch2(target, "2020-01-01T00-00-00.000000+00-00")

    entries = sonder_backup.list_backups(target)

    assert [e["path"] for e in entries] == [str(good), str(raw), str(undated)]
    assert [e["created_at_valid"] for e in entries] == [True, True, False]
    assert entries[1]["kind"] == "pre-epoch2"


def test_pre_epoch2_protection_compares_parsed_instants(isolated_state):
    target = isolated_state / "backups"
    verified = sonder_backup.create_backup(target).path
    # 10:00+02:00 is 08:00 UTC: the raw copy at 09:00 UTC is newer than the
    # verified backup even though its raw string sorts lower.
    _redate(verified, "2020-03-10T10:00:00+02:00")
    raw = _raw_pre_epoch2(target, "2020-03-10T09-00-00.000000+00-00")

    entries = sonder_backup.list_backups(target)
    assert [e["path"] for e in entries] == [str(raw), str(verified)]

    assert sonder_backup.prune_backups(target, keep=1) == []
    assert sonder_backup.prune_backups_tiered(
        target, daily=1, weekly=1, monthly=1
    ) == []
    assert raw.is_dir() and verified.is_dir()


def test_pre_epoch2_copy_superseded_by_parsed_instant_is_pruned(isolated_state):
    target = isolated_state / "backups"
    verified = sonder_backup.create_backup(target).path
    # 08:30-02:00 is 10:30 UTC, so the verified backup supersedes the 09:00
    # UTC raw copy even though its raw string sorts lower.
    _redate(verified, "2020-03-10T08:30:00-02:00")
    raw = _raw_pre_epoch2(target, "2020-03-10T09-00-00.000000+00-00")

    assert sonder_backup.prune_backups(target, keep=1) == [str(raw)]
    assert verified.is_dir() and not raw.exists()


def test_undated_newest_verified_backup_protects_every_pre_epoch2_copy(
    isolated_state,
):
    target = isolated_state / "backups"
    undated = sonder_backup.create_backup(target).path
    _strip_created_at(undated)
    raw = _raw_pre_epoch2(target, "2020-01-01T00-00-00.000000+00-00")

    # The only verified backup cannot prove it supersedes the raw copy.
    assert sonder_backup.prune_backups(target, keep=1) == []
    assert sonder_backup.prune_backups_tiered(
        target, daily=1, weekly=1, monthly=1
    ) == []
    assert raw.is_dir() and undated.is_dir()


@pytest.mark.parametrize(
    "stamp", ["0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"]
)
def test_out_of_range_offset_stamp_ranks_as_undated(isolated_state, stamp):
    target = isolated_state / "backups"
    good = sonder_backup.create_backup(target).path
    _redate(good, "2026-09-26T00:00:00Z")
    overflow = sonder_backup.create_backup(target).path
    _redate(overflow, stamp)

    entries = sonder_backup.list_backups(target)

    assert [e["path"] for e in entries] == [str(good), str(overflow)]
    assert entries[1]["created_at_valid"] is False
    assert entries[1]["created_at_utc"] == stamp


@pytest.mark.parametrize("mode", ["keep", "tiered"])
def test_prune_removes_out_of_range_offset_backup_instead_of_raising(
    isolated_state, mode
):
    target = isolated_state / "backups"
    good = sonder_backup.create_backup(target).path
    _redate(good, "2026-09-26T00:00:00Z")
    overflow = sonder_backup.create_backup(target).path
    _redate(overflow, "0001-01-01T00:00:00+01:00")

    if mode == "keep":
        removed = sonder_backup.prune_backups(target, keep=1)
    else:
        removed = sonder_backup.prune_backups_tiered(
            target, daily=1, weekly=1, monthly=1
        )

    assert removed == [str(overflow)]
    assert good.is_dir()
