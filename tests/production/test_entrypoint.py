"""SPEC-2 WP1: the single module entry point."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import sonder_migrations
import unsafe_lab

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


def test_status_storage_failure_is_resilient_and_redacted(
    isolated_home, monkeypatch, capsys
):
    from sonder_runtime.adapters import storage

    secret = "storage-secret-must-not-leak"
    monkeypatch.setattr(
        storage, "inspect_config",
        lambda config: (_ for _ in ()).throw(RuntimeError(secret)),
    )
    assert main(["status", "--json"]) == 0
    output = capsys.readouterr().out
    assert secret not in output
    payload = json.loads(output)
    assert payload["storage_error"] == (
        "RuntimeError while inspecting storage (detail suppressed)"
    )


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


def test_exported_runtime_posture_includes_proxy_declaration(monkeypatch):
    """The final HTTP bind gate must see the typed config's TLS declaration."""
    import sonder_config
    from sonder_runtime.__main__ import _export_runtime_environment

    config = sonder_config.SonderConfig(
        server=sonder_config.ServerConfig(
            host="0.0.0.0", tls_terminated_by_proxy=True
        )
    )
    # _export_runtime_environment is deliberately process-wide.  Register each
    # output with monkeypatch first so this regression fixture cannot leak its
    # non-loopback posture into the unrelated CLI tests below.
    for name in (
        "SONDER_HOST", "SONDER_PORT", "SONDER_AUTH_MODE",
        "SONDER_MAX_REQUEST_BYTES", "SONDER_TLS_TERMINATED_BY_PROXY",
        "OLLAMA_HOST", "SONDER_ALLOW_REMOTE_OLLAMA", "SONDER_WEB_TOOLS",
        "SONDER_LIVE_RELOAD",
    ):
        monkeypatch.setenv(name, os.environ.get(name, ""))
    _export_runtime_environment(config)
    assert os.environ["SONDER_TLS_TERMINATED_BY_PROXY"] == "1"


def test_mcp_entrypoint_runs_unsafe_gate_before_adapter(monkeypatch):
    import server
    from sonder_runtime.__main__ import cmd_mcp

    calls = []
    monkeypatch.setattr(
        unsafe_lab, "require_startup", lambda: calls.append("gate") or False
    )
    monkeypatch.setattr(server.mcp, "run", lambda: calls.append("mcp"))

    assert cmd_mcp(SimpleNamespace()) == 0
    assert calls == ["gate", "mcp"]


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


def test_doctor_json_skips_ollama(monkeypatch, capsys):
    import sonder_doctor

    called = []

    def check(name):
        return lambda: called.append(name) or ("ok", f"{name} checked")

    monkeypatch.setattr(
        sonder_doctor,
        "default_checks",
        lambda: [("config", check("config")), ("ollama", check("ollama"))],
    )

    assert main(["doctor", "--json", "--skip-ollama"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "overall": "ok",
        "checks": [
            {"name": "config", "status": "ok", "detail": "config checked"}
        ],
    }
    assert called == ["config"]


def test_doctor_json_fails_on_modified_migration_without_disclosure(
    monkeypatch, capsys
):
    import sonder_doctor
    import sonder_migrations
    from types import SimpleNamespace

    secret_path = "C:/Users/private/secret-memory.db"
    secret_migration = "0001_private_name"
    monkeypatch.setattr(
        sonder_migrations,
        "status_all_read_only",
        lambda home: {
            "memory": SimpleNamespace(
                applied=(secret_migration,), pending=(), unknown=(),
                checksum_mismatches=(secret_migration,), db_path=secret_path,
            )
        },
    )
    monkeypatch.setattr(
        sonder_doctor,
        "default_checks",
        lambda: [("schemas", sonder_doctor.schema_check())],
    )

    assert main(["doctor", "--json", "--skip-ollama"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "overall": "fail",
        "checks": [{
            "name": "schemas",
            "status": "fail",
            "detail": (
                "1 store(s); unhealthy history: modified=1 future=0; pending=0"
            ),
        }],
    }
    serialized = json.dumps(payload)
    assert secret_path not in serialized
    assert secret_migration not in serialized


def test_doctor_text_failure_sets_exit_code(monkeypatch, capsys):
    import sonder_doctor

    monkeypatch.setattr(
        sonder_doctor,
        "default_checks",
        lambda: [("config", lambda: ("fail", "invalid"))],
    )

    assert main(["doctor"]) == 1
    assert capsys.readouterr().out == (
        "sonder doctor: FAIL\n  [FAIL] config  invalid\n"
    )


def test_doctor_storage_probe_is_explicit(monkeypatch, isolated_home, capsys):
    import sonder_doctor

    seen = []
    monkeypatch.setattr(
        sonder_doctor,
        "default_checks",
        lambda: [("storage_state", lambda: ("ok", "automatic"))],
    )
    monkeypatch.setattr(
        sonder_doctor,
        "storage_checks",
        lambda config, throughput=False: [
            ("storage_state", lambda: seen.append(throughput) or ("ok", "probe"))
        ],
    )

    assert main(["doctor", "--json"]) == 0
    capsys.readouterr()
    assert seen == [False]
    assert main(["doctor", "--json", "--storage-probe"]) == 0
    capsys.readouterr()
    assert seen == [False, True]


def test_doctor_config_check_uses_exact_cli_config(
    isolated_home, tmp_path, capsys
):
    config = tmp_path / "sonder.toml"
    config.write_text(
        "[ollama]\nurl = 'http://127.0.0.1:11599'\n",
        encoding="utf-8",
    )
    assert main([
        "doctor", "--json", "--skip-ollama", "--config", str(config),
        "--set", "ollama.url=http://127.0.0.1:11600",
    ]) in (0, 1)
    payload = json.loads(capsys.readouterr().out)
    config_check = next(
        item for item in payload["checks"] if item["name"] == "config"
    )
    assert config_check == {
        "name": "config", "status": "ok",
        "detail": "ollama=http://127.0.0.1:11600",
    }


@pytest.mark.parametrize("selection", ["config", "set"])
def test_doctor_schema_check_uses_exact_cli_home(
    isolated_home, tmp_path, monkeypatch, capsys, selection
):
    import sonder_doctor
    import sonder_migrations

    configured_home = tmp_path / f"selected-{selection}"
    configured_home.mkdir()
    db = configured_home / "operations.db"
    sonder_migrations.migrate_store("operations", str(db))
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "UPDATE schema_migrations SET checksum_sha256 = 'tampered' "
            "WHERE migration_id = '0001_baseline'"
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(
        sonder_doctor,
        "default_checks",
        lambda: [("schemas", sonder_doctor.schema_check())],
    )
    args = ["doctor", "--json", "--skip-ollama"]
    if selection == "config":
        monkeypatch.delenv("SONDER_HOME", raising=False)
        config_path = tmp_path / "selected.toml"
        escaped_home = str(configured_home).replace("\\", "\\\\")
        config_path.write_text(
            f'[state]\nhome = "{escaped_home}"\n', encoding="utf-8"
        )
        args.extend(["--config", str(config_path)])
    else:
        args.extend(["--set", f"state.home={configured_home}"])

    assert main(args) == 1
    payload = json.loads(capsys.readouterr().out)
    # The pending count is scenery here -- this test's subject is WHICH home
    # the doctor reads -- so it is derived rather than pinned. Hard-coded, it
    # failed on every added migration, in a test that measures no migration.
    # One known migration is the deliberately-modified one, counted as
    # unhealthy rather than pending.
    known = sum(
        len(sonder_migrations.discover_migrations(store))
        for store in ("memory", "autopilot", "fleet", "operations",
                      "queued_actions", "updates")
    )
    assert payload == {
        "overall": "fail",
        "checks": [{
            "name": "schemas",
            "status": "fail",
            "detail": (
                "6 store(s); unhealthy history: modified=1 future=0; "
                "pending=%d" % (known - 1)
            ),
        }],
    }


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


def test_serve_exports_validated_config_before_migrations(
    isolated_home, tmp_path, monkeypatch, capsys
):
    """Preflighted settings must reach the runtime, and reach it in time.

    state.home/workspace_roots/ollama.url and the [features] gates were
    validated and then dropped, so the runtime used unrelated defaults; the
    home in particular must be exported before migrations open a database.
    """
    import sonder_migrations

    configured_home = tmp_path / "configured-home"
    workspace = tmp_path / "workspaces"
    workspace.mkdir()
    for name in (
        "SONDER_FILE_ROOTS", "OLLAMA_HOST", "SONDER_ALLOW_REMOTE_OLLAMA",
        "SONDER_WEB_TOOLS", "SONDER_LIVE_RELOAD", "SONDER_HOST", "SONDER_PORT",
    ):
        monkeypatch.setenv(name, os.environ.get(name, ""))

    seen: dict[str, str] = {}

    def _capture(*args, **kwargs):
        seen.update(
            home=os.environ.get("SONDER_HOME", ""),
            roots=os.environ.get("SONDER_FILE_ROOTS", ""),
            ollama=os.environ.get("OLLAMA_HOST", ""),
            web=os.environ.get("SONDER_WEB_TOOLS", ""),
        )
        raise sonder_migrations.MigrationError("stop before binding")

    monkeypatch.setattr(sonder_migrations, "migrate_all", _capture)
    rc = main(
        [
            "serve", "--skip-preflight",
            "--set", f"state.home={configured_home}",
            "--set", f"state.workspace_roots={workspace}",
            "--set", "ollama.url=http://127.0.0.1:11500",
            "--set", "features.web=true",
        ]
    )
    capsys.readouterr()
    assert rc == 1  # the stubbed migration aborts before any listener opens
    assert seen["home"] == str(configured_home)
    assert str(workspace) in seen["roots"]
    assert seen["ollama"] == "http://127.0.0.1:11500"
    assert seen["web"] == "1"


def test_serve_exports_feature_gates_closed_by_default(
    isolated_home, tmp_path, monkeypatch, capsys
):
    """[features].web=false must actually close the web-egress gate."""
    import sonder_migrations

    for name in ("SONDER_WEB_TOOLS", "SONDER_LIVE_RELOAD", "SONDER_HOST",
                 "SONDER_PORT", "OLLAMA_HOST", "SONDER_ALLOW_REMOTE_OLLAMA"):
        monkeypatch.delenv(name, raising=False)

    seen: dict[str, str] = {}

    def _capture(*args, **kwargs):
        seen.update(
            web=os.environ.get("SONDER_WEB_TOOLS", ""),
            live_reload=os.environ.get("SONDER_LIVE_RELOAD", ""),
        )
        raise sonder_migrations.MigrationError("stop before binding")

    monkeypatch.setattr(sonder_migrations, "migrate_all", _capture)
    assert main(["serve", "--skip-preflight"]) == 1
    capsys.readouterr()
    assert seen["web"] == "0"
    assert seen["live_reload"] == "0"


def test_migrate_exports_configured_home(isolated_home, tmp_path, monkeypatch):
    import sonder_migrations

    configured_home = tmp_path / "migration-home"
    seen = {}

    def migrate_all():
        seen["home"] = os.environ.get("SONDER_HOME")
        return {}

    monkeypatch.setattr(sonder_migrations, "migrate_all", migrate_all)
    assert main(["migrate", "--set", f"state.home={configured_home}"]) == 0
    assert seen["home"] == str(configured_home)


def test_backup_fails_closed_on_invalid_config(isolated_home, monkeypatch, capsys):
    import sonder_backup

    called = []
    monkeypatch.setattr(sonder_backup, "list_backups", lambda target: called.append(target) or [])
    assert main(["backup", "list", "--set", "server.port=not-a-port"]) == 2
    capsys.readouterr()
    assert called == []


def test_backup_exports_configured_source_home(
    isolated_home, tmp_path, monkeypatch, capsys
):
    import sonder_backup

    configured_home = tmp_path / "backup-home"
    target = tmp_path / "target"
    seen = {}

    def create_backup(path):
        seen["home"] = os.environ.get("SONDER_HOME")
        return SimpleNamespace(
            backup_id="backup-test", path=target / "backup-test",
            file_count=0, total_bytes=0,
        )

    monkeypatch.setattr(sonder_backup, "create_backup", create_backup)
    assert main([
        "backup", "create", "--target", str(target),
        "--set", f"state.home={configured_home}", "--json",
    ]) == 0
    capsys.readouterr()
    assert seen["home"] == str(configured_home)


def test_smoke_exports_the_home_it_preflighted(
    isolated_home, tmp_path, monkeypatch, capsys
):
    import sonder_migrations

    configured_home = tmp_path / "smoke-home"
    seen = {}

    def migrate_store(name):
        seen["home"] = os.environ.get("SONDER_HOME")
        raise RuntimeError("stop after path capture")

    monkeypatch.setattr(sonder_migrations, "migrate_store", migrate_store)
    assert main([
        "smoke", "--skip-ollama",
        "--set", "state.minimum_free_disk_bytes=0",
        "--set", f"state.home={configured_home}",
    ]) == 1
    capsys.readouterr()
    assert seen["home"] == str(configured_home)


def test_backup_verify_json_flag_emits_json(isolated_home, tmp_path, capsys):
    assert main(["migrate", "--store", "operations"]) == 0
    capsys.readouterr()
    target = tmp_path / "backups"
    assert main(["backup", "create", "--target", str(target), "--json"]) == 0
    backup_dir = json.loads(capsys.readouterr().out)["path"]

    assert main(["backup", "verify", backup_dir, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True and payload["problems"] == []

    assert main(["restore", "verify", backup_dir, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True

    assert main(["restore", "smoke", backup_dir, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


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
