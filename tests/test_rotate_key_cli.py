"""Regression tests: ``rotate-key`` honours the selected secrets file and home.

It used to read only ``--secrets`` (refusing when ``SONDER_SECRETS`` was set),
ignore ``--config``/``--set`` so the rotation state and the audit event went
to the environment's home, and misreport a directory as "group/world
accessible".
"""
from __future__ import annotations

import os
import sqlite3

import pytest

from sonder_runtime.__main__ import main

# Assembled at runtime so no literal key-shaped string lives in the fixture.
_OLD_KEY = "old-rotation-" + "value-" + "0123456789abcdef"


@pytest.fixture
def env_home(tmp_path, monkeypatch):
    path = tmp_path / "env-home"
    path.mkdir()
    monkeypatch.setenv("SONDER_HOME", str(path))
    for name in (
        "SONDER_CONFIG", "SONDER_SECRETS", "SONDER_ROTATION_STATE", "SONDER_DB",
        "SONDER_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    return path


@pytest.fixture
def secrets_file(tmp_path):
    path = tmp_path / "sonder.env"
    path.write_text("SONDER_API_KEY=%s\n" % _OLD_KEY, encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def _rotated_events(home):
    db = home / "operations.db"
    if not db.exists():
        return []
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT event_code FROM operation_event "
            "WHERE event_code='API_KEY_ROTATED'"
        ).fetchall()
    finally:
        conn.close()


def test_sonder_secrets_env_is_honoured(env_home, secrets_file, monkeypatch, capsys):
    monkeypatch.setenv("SONDER_SECRETS", str(secrets_file))

    rc = main(["rotate-key", "--json", "--overlap-seconds", "60"])

    assert rc == 0, capsys.readouterr().err
    assert _OLD_KEY not in secrets_file.read_text(encoding="utf-8")


def test_set_state_home_receives_rotation_state_and_audit(
    env_home, secrets_file, tmp_path, capsys
):
    selected = tmp_path / "selected-home"
    selected.mkdir()

    rc = main([
        "rotate-key", "--json", "--secrets", str(secrets_file),
        "--set", "state.home=%s" % selected, "--overlap-seconds", "60",
    ])

    assert rc == 0, capsys.readouterr().err
    assert (selected / "secrets" / "rotation.json").is_file()
    assert not (env_home / "secrets" / "rotation.json").exists()
    assert _rotated_events(selected)
    assert not _rotated_events(env_home)


def test_invalid_config_is_refused_before_rotation(
    env_home, secrets_file, tmp_path, capsys
):
    rc = main([
        "rotate-key", "--secrets", str(secrets_file),
        "--config", str(tmp_path / "missing.toml"),
    ])

    assert rc == 2
    assert _OLD_KEY in secrets_file.read_text(encoding="utf-8")


def test_missing_secrets_everywhere_is_a_usage_error(env_home, capsys):
    rc = main(["rotate-key"])
    assert rc == 2
    assert "SONDER_SECRETS" in capsys.readouterr().err


def test_directory_is_not_reported_as_group_world_accessible(tmp_path, monkeypatch):
    import sonder_runtime.adapters.secrets as sonder_secrets

    monkeypatch.setenv("SONDER_ROTATION_STATE", str(tmp_path / "rotation.json"))
    directory = tmp_path / "secrets-dir"
    directory.mkdir(mode=0o700)

    with pytest.raises(sonder_secrets.RotationError) as excinfo:
        sonder_secrets.rotate_api_key(directory)

    assert "not a regular file" in str(excinfo.value)
    assert "group/world" not in str(excinfo.value)
