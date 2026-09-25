"""Regression tests for CLI output contracts found by live operator testing.

* ``--json`` output must be exactly one JSON document on stdout; ``restore
  apply --json`` appended a prose line and ``smoke --json`` ignored the flag.
* Expected operator faults (non-empty restore destination, tampered backup,
  a file where a backup directory is expected, ``prune --keep 0``) must be a
  one-line error and a non-zero exit, not a Python traceback.
* ``--config``/``--secrets`` pointing at a directory or undecodable bytes
  must be a configuration error (exit 2), not a traceback.
"""
from __future__ import annotations

import json
import os

import pytest

from sonder_runtime.__main__ import main


@pytest.fixture
def home(tmp_path, monkeypatch):
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("SONDER_HOME", str(path))
    for name in (
        "SONDER_CONFIG", "SONDER_SECRETS", "SONDER_DB", "SONDER_OPERATIONS_DB",
        "SONDER_AUTOPILOT_DB", "SONDER_FLEET_DB", "SONDER_RUNTIME_POLICY",
    ):
        monkeypatch.delenv(name, raising=False)
    assert main(["migrate", "--json"]) == 0
    return path


def _backup(home, capsys):
    capsys.readouterr()
    assert main(["backup", "create", "--json"]) == 0
    return json.loads(capsys.readouterr().out)["path"]


def test_restore_apply_json_is_a_single_document(home, tmp_path, capsys):
    backup = _backup(home, capsys)
    dest = tmp_path / "restored"
    rc = main(["restore", "apply", backup, str(dest), "--confirm", "restore",
               "--json"])
    out = capsys.readouterr()
    assert rc == 0, out.err
    payload = json.loads(out.out)
    assert payload["restored"]
    assert "SONDER_HOME" in out.err  # the operator hint is still shown


def test_restore_apply_text_keeps_the_hint_on_stdout(home, tmp_path, capsys):
    backup = _backup(home, capsys)
    rc = main(["restore", "apply", backup, str(tmp_path / "r"),
               "--confirm", "restore"])
    assert rc == 0
    assert "State restored" in capsys.readouterr().out


def test_restore_apply_into_non_empty_destination_is_a_clean_error(
    home, tmp_path, capsys
):
    backup = _backup(home, capsys)
    dest = tmp_path / "occupied"
    dest.mkdir()
    (dest / "keep.txt").write_text("user data", encoding="utf-8")
    rc = main(["restore", "apply", backup, str(dest), "--confirm", "restore"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "Traceback" not in err
    assert "not empty" in err
    assert (dest / "keep.txt").read_text(encoding="utf-8") == "user data"


def test_restore_apply_of_missing_backup_is_a_clean_error(
    home, tmp_path, capsys
):
    rc = main(["restore", "apply", str(tmp_path / "missing"),
               str(tmp_path / "dest"), "--confirm", "restore"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "Traceback" not in err and "restore failed" in err
    assert not (tmp_path / "dest").exists()


def test_backup_create_onto_a_file_is_a_clean_error(home, tmp_path, capsys):
    target = tmp_path / "a-file"
    target.write_text("x", encoding="utf-8")
    rc = main(["backup", "create", "--target", str(target)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "Traceback" not in err and "backup failed" in err


@pytest.mark.parametrize("keep", ["0", "-1"])
def test_backup_prune_keep_below_one_is_a_clean_error(home, capsys, keep):
    _backup(home, capsys)
    rc = main(["backup", "prune", "--keep", keep])
    err = capsys.readouterr().err
    assert rc == 1
    assert "Traceback" not in err and "at least one" in err


def _gateway_records(caplog):
    return [
        record for record in caplog.records
        if record.name == "sonder_runtime.adapters.backup_gateway"
        and record.levelno >= 40
    ]


def test_expected_backup_faults_are_logged_without_a_traceback(
    home, tmp_path, capsys, caplog
):
    # The CLI has no logging handler, so Python's last-resort handler printed
    # every exc_info record as a raw traceback ahead of the clean message.
    target = tmp_path / "a-file"
    target.write_text("x", encoding="utf-8")
    caplog.set_level("ERROR")
    assert main(["backup", "create", "--target", str(target)]) == 1
    assert main(["restore", "apply", str(tmp_path / "missing"),
                 str(tmp_path / "dest"), "--confirm", "restore"]) == 1
    records = _gateway_records(caplog)
    assert len(records) == 2
    assert not any(record.exc_info for record in records)
    assert "backup create failed" in records[0].getMessage()
    assert "restore refused" in records[1].getMessage()


def test_unexpected_backup_faults_keep_their_traceback(
    home, tmp_path, monkeypatch, caplog
):
    import sonder_runtime.adapters.backup as backup_impl

    def boom(_target):
        raise ZeroDivisionError("defect")

    monkeypatch.setattr(backup_impl, "create_backup", boom)
    caplog.set_level("ERROR")
    with pytest.raises(ZeroDivisionError):
        main(["backup", "create"])
    records = _gateway_records(caplog)
    assert records and records[0].exc_info is not None


def test_concurrent_backup_lock_is_a_clean_error(home, monkeypatch, capsys):
    import sonder_runtime.adapters.backup as backup_impl
    from sonder_runtime.adapters.persistence.operations_store import (
        MaintenanceLockHeld,
    )

    def held(_target):
        raise MaintenanceLockHeld("backup", "backup-1", "backup in progress")

    monkeypatch.setattr(backup_impl, "create_backup", held)
    assert main(["backup", "create"]) == 1
    err = capsys.readouterr().err
    assert "backup failed" in err and "backup in progress" in err


def test_smoke_json_emits_a_document(home, capsys):
    capsys.readouterr()
    rc = main(["smoke", "--skip-ollama", "--json",
               "--set", "state.minimum_free_disk_bytes=0"])
    out = capsys.readouterr()
    assert rc == 0, out.err
    assert json.loads(out.out) == {"ok": True, "failures": []}


def test_smoke_json_reports_failures_in_the_document(home, capsys):
    capsys.readouterr()
    rc = main(["smoke", "--json", "--set", "ollama.url=http://127.0.0.1:1",
               "--set", "state.minimum_free_disk_bytes=0"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["ok"] is False
    assert any(item.startswith("preflight: ollama") for item in payload["failures"])


@pytest.mark.parametrize("flag", ["--config", "--secrets"])
@pytest.mark.parametrize("command", ["config", "doctor", "preflight"])
def test_config_paths_that_are_directories_are_config_errors(
    home, tmp_path, capsys, flag, command
):
    directory = tmp_path / "a-directory"
    directory.mkdir(mode=0o700)
    rc = main([command, flag, str(directory)])
    err = capsys.readouterr().err
    assert rc == 2
    assert "Traceback" not in err
    assert "invalid configuration" in err


def test_undecodable_config_is_a_config_error(home, tmp_path, capsys):
    config = tmp_path / "binary.toml"
    config.write_bytes(b"\x8d\xff\xfe[server]\n")
    rc = main(["config", "--config", str(config)])
    err = capsys.readouterr().err
    assert rc == 2
    assert "invalid configuration" in err and "Traceback" not in err


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_unreadable_config_is_a_config_error(home, tmp_path, capsys):
    if os.geteuid() == 0:
        pytest.skip("root bypasses file permissions")
    config = tmp_path / "locked.toml"
    config.write_text("[server]\n", encoding="utf-8")
    config.chmod(0)
    rc = main(["config", "--config", str(config)])
    assert rc == 2
    assert "invalid configuration" in capsys.readouterr().err
