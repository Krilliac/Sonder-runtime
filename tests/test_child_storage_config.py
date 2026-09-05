from dataclasses import replace

import pytest

from sonder_runtime.platform.child_storage_config import (
    ChildStorageConfig,
    child_storage_errors,
)
from sonder_runtime.platform.config import SonderConfig


def test_sqlite_default_needs_no_optional_driver_or_credentials():
    config = SonderConfig()
    assert config.child_storage.backend == "sqlite"
    assert child_storage_errors(config) == []


@pytest.mark.parametrize(
    "change",
    [
        {"backend": "unknown"},
        {"pool_size": 0},
        {"pool_size": 5},
        {"pool_size": True},
        {"operation_timeout_seconds": 0},
        {"cancel_timeout_seconds": 6},
        {"backend": "postgresql"},
    ],
)
def test_invalid_storage_configuration_fails_closed(change):
    config = replace(
        SonderConfig(), child_storage=replace(ChildStorageConfig(), **change)
    )
    assert child_storage_errors(config)


def test_postgres_pair_requires_fixed_owner_binding_and_standby(tmp_path):
    section = ChildStorageConfig(
        backend="postgresql",
        owner_id="node-a",
        binding_file=str(tmp_path / "binding.json"),
        durability="sync-pair",
    )
    config = replace(SonderConfig(), child_storage=section)
    assert child_storage_errors(config)
    assert (
        child_storage_errors(
            replace(
                config, child_storage=replace(section, required_standby="standby_a")
            )
        )
        == []
    )


def test_connection_binding_is_not_exposed_in_redacted_config(tmp_path):
    section = ChildStorageConfig(binding_file=str(tmp_path / "private-binding.json"))
    config = replace(SonderConfig(), child_storage=section)
    assert str(tmp_path) not in str(config.as_redacted_dict()["child_storage"])


def test_legacy_composition_cannot_ignore_explicit_postgres_environment(monkeypatch):
    from sonder_runtime.bootstrap.app import build_application
    from sonder_runtime.platform.config import ConfigError

    monkeypatch.setenv("SONDER_CHILD_STORAGE_BACKEND", "postgresql")
    monkeypatch.delenv("SONDER_CHILD_STORAGE_BINDING_FILE", raising=False)
    with pytest.raises(ConfigError):
        build_application()


def test_explicit_postgres_failure_never_constructs_sqlite(monkeypatch, tmp_path):
    from sonder_runtime.bootstrap.app import build_application
    from sonder_runtime.adapters.persistence import durable_continuation

    monkeypatch.setattr(
        durable_continuation,
        "SQLiteDurableContinuationRepository",
        lambda *args: pytest.fail("PostgreSQL failure fell back to SQLite"),
    )
    config = replace(
        SonderConfig(),
        child_storage=ChildStorageConfig(
            backend="postgresql",
            owner_id="host-a",
            binding_file=str(tmp_path / "missing-binding.json"),
        ),
    )
    with pytest.raises(Exception):
        build_application(config=config)
