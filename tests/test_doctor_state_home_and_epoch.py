"""Regression tests: doctor inspects the selected home's memory DB and epoch.

* ``self_heal``/``memory_quality`` were always ``skipped: SONDER_DB not set``
  from the packaged CLI, although the state home locates ``memory.db`` and the
  REPL's checks work on the same home.
* doctor had no schema-epoch check, so an un-adopted home reported WARN/rc 0
  while ``serve`` refused to start with "migration required".

Both checks must stay read-only: doctor never creates, initializes or migrates
a database.
"""
from __future__ import annotations

import json
import os

import pytest

import sonder_doctor
from sonder_runtime.__main__ import main


@pytest.fixture
def home(tmp_path, monkeypatch):
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("SONDER_HOME", str(path))
    for name in ("SONDER_CONFIG", "SONDER_SECRETS", "SONDER_DB", "OLLAMA_HOST"):
        monkeypatch.delenv(name, raising=False)
    return path


def _doctor(capsys, *extra):
    rc = main(["doctor", "--json", "--skip-ollama", *extra])
    payload = json.loads(capsys.readouterr().out)
    return rc, {c["name"]: c for c in payload["checks"]}, payload


def _adopt(capsys):
    assert main(["migrate", "--adopt-epoch2", "--json"]) == 0
    assert main(["migrate", "--json"]) == 0
    capsys.readouterr()


def _snapshot(path):
    return {
        p.name: (p.stat().st_size, p.stat().st_mtime_ns)
        for p in path.iterdir() if p.is_file()
    }


def test_fresh_home_fails_schema_epoch_and_writes_nothing(home, capsys):
    rc, checks, payload = _doctor(capsys)

    assert rc == 1
    assert payload["overall"] == "fail"
    epoch = checks["schema_epoch"]
    assert epoch["status"] == "fail"
    assert "migrate --adopt-epoch2" in epoch["detail"]
    # No memory.db exists: reported as skipped, not created behind our back.
    assert checks["self_heal"]["status"] == "skipped"
    assert "SONDER_DB" not in checks["self_heal"]["detail"]
    assert not (home / "memory.db").exists()
    for name in ("automation.db", "selfmod.db", "training.db"):
        assert not (home / name).exists()


def test_adopted_home_derives_memory_db_from_state_home(home, capsys):
    _adopt(capsys)
    before = _snapshot(home)

    rc, checks, payload = _doctor(capsys)

    assert checks["schema_epoch"]["status"] == "ok", checks["schema_epoch"]
    assert checks["self_heal"]["status"] == "ok", checks["self_heal"]
    assert checks["memory_quality"]["status"] == "ok", checks["memory_quality"]
    assert "SONDER_DB not set" not in json.dumps(payload)
    assert rc == 0, payload
    # Read-only: no database in the home was touched.
    after = _snapshot(home)
    assert {k: v for k, v in after.items() if k.endswith(".db")} == {
        k: v for k, v in before.items() if k.endswith(".db")
    }


def test_set_selects_the_home_whose_memory_db_is_inspected(
    home, tmp_path, capsys, monkeypatch
):
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("SONDER_HOME", str(other))
    _adopt(capsys)
    monkeypatch.setenv("SONDER_HOME", str(home))

    rc, checks, _ = _doctor(capsys, "--set", "state.home=%s" % other)

    assert checks["schema_epoch"]["status"] == "ok"
    assert checks["memory_quality"]["status"] == "ok"
    assert rc == 0


def test_explicit_sonder_db_still_wins(home, tmp_path, capsys, monkeypatch):
    _adopt(capsys)
    missing = tmp_path / "elsewhere" / "memory.db"
    monkeypatch.setenv("SONDER_DB", str(missing))

    _rc, checks, _ = _doctor(capsys)

    assert checks["memory_quality"]["status"] == "skipped"
    assert "SONDER_DB" in checks["memory_quality"]["detail"]
    assert not missing.exists()


def test_uninitialized_memory_db_is_skipped_not_initialized(home, capsys):
    import sqlite3

    db = home / "memory.db"
    sqlite3.connect(db).close()
    size = db.stat().st_size

    _rc, checks, _ = _doctor(capsys)

    assert checks["memory_quality"]["status"] == "skipped"
    assert checks["self_heal"]["status"] == "skipped"
    assert db.stat().st_size == size
    assert not os.path.exists(str(db) + "-wal")


def test_future_epoch_is_a_failure(home, capsys, monkeypatch):
    from sonder_runtime.adapters.persistence.sqlite import bridge_migration

    monkeypatch.setattr(bridge_migration, "check_epoch", lambda _path: 3)
    check = sonder_doctor.schema_epoch_check(
        type("C", (), {"state": type("S", (), {"home": str(home)})()})()
    )
    result = check()
    assert result["status"] == "fail"
    assert "future schema epoch" in result["detail"]


def test_default_registry_includes_schema_epoch():
    names = [name for name, _ in sonder_doctor.default_checks()]
    assert names.index("schema_epoch") == names.index("schemas") + 1
