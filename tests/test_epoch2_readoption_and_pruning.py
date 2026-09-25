"""Regression tests for repeated ``migrate --adopt-epoch2`` and its backups.

* Re-running an already adopted home took a new full ``pre-epoch2-*`` copy
  every time; those copies were invisible to ``backup list``/``prune`` and
  accumulated forever.
* ``--store`` was accepted and silently ignored alongside ``--adopt-epoch2``.
"""
from __future__ import annotations

import json
import time

import pytest

from sonder_runtime.__main__ import main
from sonder_runtime.adapters import backup as backup_adapter


@pytest.fixture
def home(tmp_path, monkeypatch):
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("SONDER_HOME", str(path))
    for name in ("SONDER_CONFIG", "SONDER_SECRETS", "SONDER_DB"):
        monkeypatch.delenv(name, raising=False)
    return path


def _adopt(capsys):
    rc = main(["migrate", "--adopt-epoch2", "--json"])
    out = capsys.readouterr().out
    assert rc == 0, out
    return json.loads(out)


def _pre_epoch2(home):
    return sorted((home / "backups").glob("pre-epoch2-*"))


def test_readoption_is_a_verified_noop_without_a_new_backup(home, capsys):
    assert main(["migrate", "--json"]) == 0  # pre-existing epoch-1 state
    capsys.readouterr()
    first = _adopt(capsys)
    assert first["already_adopted"] is False
    copies = _pre_epoch2(home)
    assert len(copies) == 1
    receipt = (home / "epoch2_adoption_receipt.json").read_bytes()

    second = _adopt(capsys)
    third = _adopt(capsys)

    assert _pre_epoch2(home) == copies
    assert (home / "epoch2_adoption_receipt.json").read_bytes() == receipt
    for payload in (second, third):
        assert payload["already_adopted"] is True
        assert payload["verified"] is True
        assert payload["backup_path"] == first["backup_path"]


def test_partial_adoption_still_reruns_the_crash_safe_bridge(home, capsys):
    _adopt(capsys)
    (home / "epoch2_adoption_receipt.json").unlink()

    payload = _adopt(capsys)

    assert payload["already_adopted"] is False
    assert (home / "epoch2_adoption_receipt.json").is_file()


def test_store_with_adopt_epoch2_is_a_usage_error(home, capsys):
    rc = main(["migrate", "--adopt-epoch2", "--store", "memory"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "--store cannot be combined with --adopt-epoch2" in err
    assert not (home / "epoch2_adoption_receipt.json").exists()


def _fake_pre_epoch2(target, stamp):
    path = target / ("pre-epoch2-" + stamp)
    path.mkdir(parents=True)
    (path / "memory.db").write_bytes(b"")
    return path


def test_pre_epoch2_copies_are_listed(tmp_path):
    copy = _fake_pre_epoch2(tmp_path, "2026-01-02T03-04-05.123456+00-00")
    (tmp_path / "pre-epoch2-garbage").mkdir()  # undatable: never touched

    entries = backup_adapter.list_backups(tmp_path)

    assert entries == [{
        "path": str(copy),
        "backup_id": copy.name,
        "created_at_utc": "2026-01-02T03:04:05.123456Z",
        "application_version": "unknown",
        "files": 1,
        "kind": "pre-epoch2",
    }]


def test_prune_keeps_pre_epoch2_until_a_newer_verified_backup_exists(
    home, tmp_path, capsys
):
    target = tmp_path / "target"
    old = _fake_pre_epoch2(target, "2020-01-01T00-00-00.000000+00-00")
    newer = _fake_pre_epoch2(target, "2020-02-01T00-00-00.000000+00-00")

    # No verified standard backup: every pre-epoch2 copy is a recovery point.
    assert backup_adapter.prune_backups(target, keep=1) == []
    assert old.is_dir() and newer.is_dir()

    _adopt(capsys)
    assert main(["migrate", "--json"]) == 0
    capsys.readouterr()
    backup_adapter.create_backup(target)
    time.sleep(0.01)

    removed = backup_adapter.prune_backups(target, keep=1)

    assert sorted(removed) == sorted([str(old), str(newer)])
    remaining = backup_adapter.list_backups(target)
    assert len(remaining) == 1 and "kind" not in remaining[0]


def test_tiered_prune_includes_old_pre_epoch2_copies(home, tmp_path, capsys):
    target = tmp_path / "target"
    old = _fake_pre_epoch2(target, "2019-06-01T00-00-00.000000+00-00")
    _adopt(capsys)
    assert main(["migrate", "--json"]) == 0
    capsys.readouterr()
    backup_adapter.create_backup(target)

    removed = backup_adapter.prune_backups_tiered(
        target, daily=1, weekly=1, monthly=1
    )

    assert removed == [str(old)]
