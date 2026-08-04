"""SPEC-2 WP1: the single module entry point."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from sonder_runtime.__main__ import main

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("SONDER_HOME", str(home))
    monkeypatch.setenv("SONDER_OPERATIONS_DB", str(home / "operations.db"))
    monkeypatch.setenv("SONDER_DB", str(home / "memory.db"))
    monkeypatch.setenv("SONDER_AUTOPILOT_DB", str(home / "autopilot.db"))
    monkeypatch.setenv("SONDER_FLEET_DB", str(home / "fleet.db"))
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(home / "runtime_policy.json"))
    return home


def test_status_json(isolated_home, capsys):
    assert main(["status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["build"]["version"]
    assert "schemas" in payload


def test_config_show_is_redacted(isolated_home, capsys, monkeypatch):
    monkeypatch.setenv("SONDER_API_KEY", "super-secret-key-value-000111")
    assert main(["config", "--json"]) == 0
    out = capsys.readouterr().out
    assert "super-secret-key-value-000111" not in out
    payload = json.loads(out)
    assert payload["secrets"]["api_key"] == "[set]"


def test_invalid_security_config_fails_before_bind(isolated_home, capsys):
    # `serve` with a non-loopback host and no TLS proxy declaration must
    # exit with a configuration error without ever opening a listener.
    rc = main(["serve", "--set", "server.host=0.0.0.0"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "tls_terminated_by_proxy" in err


def test_migrate_and_smoke(isolated_home, capsys):
    assert main(["migrate", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["operations"]["applied"] == ["0001_baseline"]

    rc = main(
        [
            "smoke",
            "--skip-ollama",
            "--set",
            "state.minimum_free_disk_bytes=0",
        ]
    )
    out = capsys.readouterr()
    assert rc == 0, out.err
    assert "smoke passed" in out.out


def test_preflight_reports_json(isolated_home, capsys):
    rc = main(
        [
            "preflight",
            "--json",
            "--skip-ollama",
            "--set",
            "state.minimum_free_disk_bytes=0",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0, payload
    assert payload["ok"] is True
    names = {c["name"] for c in payload["checks"]}
    assert {"state_home_writable", "disk_space", "runtime_policy"} <= names


def test_diagnostics_redacts_all_known_secrets(isolated_home, capsys, monkeypatch):
    secret = "diagnostic-secret-abcdef-9988"
    monkeypatch.setenv("SONDER_API_KEY", secret)
    monkeypatch.setenv("SONDER_AUTH_SECRET", secret + "-auth")
    assert main(["diagnostics", "--skip-ollama"]) == 0
    out = capsys.readouterr().out
    assert secret not in out


def test_module_executes_as_subprocess(isolated_home):
    env = dict(os.environ)
    result = subprocess.run(
        [sys.executable, "-m", "sonder_runtime", "--version"],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "sonder-runtime" in result.stdout


def test_backup_and_restore_via_cli(isolated_home, tmp_path, capsys):
    assert main(["migrate", "--store", "operations"]) == 0
    capsys.readouterr()
    target = tmp_path / "backups"
    assert main(["backup", "create", "--target", str(target), "--json"]) == 0
    created = json.loads(capsys.readouterr().out)
    backup_dir = created["path"]

    assert main(["backup", "verify", backup_dir]) == 0
    capsys.readouterr()

    dest = tmp_path / "restored"
    rc = main(["restore", "apply", backup_dir, str(dest)])
    assert rc == 2  # refused without --confirm
    capsys.readouterr()
    rc = main(
        ["restore", "apply", backup_dir, str(dest), "--confirm", "restore"]
    )
    assert rc == 0
    assert (dest / "operations.db").exists()
