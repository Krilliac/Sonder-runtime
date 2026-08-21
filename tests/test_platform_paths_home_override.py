from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import sonder_runtime.platform.paths as paths


@pytest.fixture(autouse=True)
def reset_process_home():
    paths.reset_home()
    yield
    paths.reset_home()


def test_configured_home_precedes_home_and_state_environment(monkeypatch, tmp_path):
    configured = tmp_path / "configured"
    monkeypatch.setenv("SONDER_HOME", str(tmp_path / "environment"))
    monkeypatch.setenv("SONDER_STATE", str(tmp_path / "state-environment"))

    paths.configure_home(configured)

    assert paths.default_home() == configured
    assert paths.state_path("jobs.db", "SONDER_STATE") == str(configured / "jobs.db")
    assert configured.is_dir()


def test_reset_home_restores_legacy_environment_behavior(monkeypatch, tmp_path):
    environment_home = tmp_path / "environment"
    monkeypatch.setenv("SONDER_HOME", str(environment_home))
    paths.configure_home(tmp_path / "configured")
    paths.reset_home()

    assert paths.default_home() == environment_home


def test_home_override_is_process_local_and_does_not_export_path(monkeypatch, tmp_path):
    sentinel = "environment-value-that-must-remain"
    configured = tmp_path / "private" / "secret-looking-home"
    monkeypatch.setenv("SONDER_HOME", sentinel)

    result = paths.configure_home(configured)

    assert result is None
    assert paths.default_home() == configured
    assert __import__("os").environ["SONDER_HOME"] == sentinel


def test_home_override_rejects_empty_values_without_echoing_input():
    with pytest.raises(ValueError, match="^home must not be empty$") as error:
        paths.configure_home("   ")

    assert "secret" not in str(error.value).lower()


def test_home_override_lookups_are_safe_under_concurrent_configure_and_reset(
    monkeypatch, tmp_path
):
    fallback = tmp_path / "fallback"
    configured = tuple(tmp_path / f"configured-{index}" for index in range(8))
    monkeypatch.setenv("SONDER_HOME", str(fallback))

    def lookup(index):
        if index % 3 == 0:
            paths.reset_home()
        else:
            paths.configure_home(configured[index % len(configured)])
        return Path(paths.default_home())

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lookup, range(400)))

    assert set(results) <= {fallback, *configured}
