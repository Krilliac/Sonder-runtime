"""SPEC-2 section 4/WP3: preflight gates and degraded reporting."""
from __future__ import annotations

import pytest

import sonder_config
from sonder_runtime.adapters import preflight as sonder_preflight

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


def test_preflight_passes_without_ollama_probe(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "SONDER_RUNTIME_POLICY", str(tmp_path / "home" / "runtime_policy.json")
    )
    config = _config(tmp_path)
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
