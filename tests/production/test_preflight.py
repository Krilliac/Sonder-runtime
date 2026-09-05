"""SPEC-2 section 4/WP3: preflight gates and degraded reporting."""
from __future__ import annotations

import json
from urllib.error import URLError

import pytest

import sonder_config
from sonder_runtime.adapters import preflight as sonder_preflight
from sonder_runtime.adapters.persistence import migrations
from sonder_runtime.platform import paths as runtime_paths

pytestmark = pytest.mark.unit


def _config(tmp_path, **env):
    base_env = {
        "SONDER_HOME": str(tmp_path / "home"),
        "SONDER_OPERATIONS_DB": str(tmp_path / "home" / "operations.db"),
        "SONDER_RUNTIME_POLICY": str(tmp_path / "home" / "runtime_policy.json"),
    }
    base_env.update(env)
    return sonder_config.load_config(
        env=base_env, overrides={"state.minimum_free_disk_bytes": "0"}
    )


@pytest.mark.parametrize("prior_schema_applied", [False, True])
def test_preflight_passes_without_ollama_probe(
    tmp_path, monkeypatch, prior_schema_applied
):
    # A previous application may have migrated its own home in this process.
    previous_home = tmp_path / "previous-home"
    runtime_paths.configure_home(previous_home)
    monkeypatch.setenv("SONDER_DB", str(previous_home / "memory.db"))
    if prior_schema_applied:
        migrations.migrate_all()
    monkeypatch.setenv(
        "SONDER_RUNTIME_POLICY", str(tmp_path / "home" / "runtime_policy.json")
    )
    config = _config(tmp_path)
    # Match application startup: schema adapters use the selected runtime home.
    runtime_paths.configure_home(config.state.home)
    monkeypatch.setenv("SONDER_DB", str(tmp_path / "home" / "memory.db"))
    assert migrations.store_db_paths()["operations"] == str(
        tmp_path / "home" / "operations.db"
    )
    report = sonder_preflight.run_preflight(config, check_ollama=False)
    assert report.ok, [c.as_dict() for c in report.checks if not c.ok]
    # operations baseline is pending on a fresh home: degraded, not failed.
    assert report.degraded


def test_unreachable_ollama_fails_required_check(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "SONDER_RUNTIME_POLICY", str(tmp_path / "home" / "runtime_policy.json")
    )
    config = _config(tmp_path, OLLAMA_HOST="127.0.0.1:1")
    report = sonder_preflight.run_preflight(
        config, check_ollama=True, ollama_timeout=0.5
    )
    assert not report.ok
    ollama = next(c for c in report.checks if c.name == "ollama")
    assert not ollama.ok and ollama.required


def test_missing_workspace_root_fails(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "SONDER_RUNTIME_POLICY", str(tmp_path / "home" / "runtime_policy.json")
    )
    config = _config(
        tmp_path, SONDER_FILE_ROOTS=str(tmp_path / "does-not-exist")
    )
    report = sonder_preflight.run_preflight(config, check_ollama=False)
    assert not report.ok
    failing = [c for c in report.checks if not c.ok and c.required]
    assert any("workspace_root" in c.name for c in failing)


def test_insufficient_disk_fails(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "SONDER_RUNTIME_POLICY", str(tmp_path / "home" / "runtime_policy.json")
    )
    config = sonder_config.load_config(
        env={
            "SONDER_HOME": str(tmp_path / "home"),
            "SONDER_OPERATIONS_DB": str(tmp_path / "home" / "operations.db"),
        },
        overrides={
            "state.minimum_free_disk_bytes": str(1 << 60)  # 1 EiB
        },
    )
    report = sonder_preflight.run_preflight(config, check_ollama=False)
    disk = next(c for c in report.checks if c.name == "disk_space")
    assert not disk.ok and disk.required
    assert not report.ok


def test_optional_worker_outage_is_reported_as_degraded_not_failed(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv(
        "SONDER_RUNTIME_POLICY", str(tmp_path / "home" / "runtime_policy.json")
    )
    config = _config(
        tmp_path,
        SONDER_ALLOW_REMOTE_OLLAMA="1",
        SONDER_OLLAMA_WORKERS="https://worker.example:443",
    )

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return json.dumps({"models": [{"name": "local:latest"}]}).encode()

    def open_url(request, timeout):
        del timeout
        if "worker.example" in request.full_url:
            raise URLError("worker offline")
        return Response()

    monkeypatch.setattr(sonder_preflight.urllib.request, "urlopen", open_url)
    report = sonder_preflight.run_preflight(config, ollama_timeout=0.1)

    worker = next(row for row in report.checks if row.name == "ollama_worker_1")
    assert report.ok
    assert report.degraded
    assert not worker.ok and not worker.required
