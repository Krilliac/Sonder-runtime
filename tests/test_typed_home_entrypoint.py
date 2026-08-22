from __future__ import annotations

import pytest

from sonder_runtime.__main__ import main
from sonder_runtime.adapters.persistence import migrations
from sonder_runtime.bootstrap import app as bootstrap_app
from sonder_runtime.platform import paths
from sonder_runtime.platform.config import SonderConfig, StateConfig


@pytest.fixture(autouse=True)
def reset_typed_home():
    paths.reset_home()
    yield
    paths.reset_home()


def test_build_application_binds_typed_home_before_lazy_state_paths(
    tmp_path, monkeypatch
):
    poisoned = tmp_path / "poisoned"
    configured = tmp_path / "configured"
    monkeypatch.setenv("SONDER_HOME", str(poisoned))
    config = SonderConfig(
        state=StateConfig(home=str(configured)),
    )

    application = bootstrap_app.build_application(config=config)

    assert application.config is config
    assert paths.state_path("jobs.db", "SONDER_JOBS_DB") == str(
        configured / "jobs.db"
    )
    assert not (poisoned / "jobs.db").exists()


def test_build_application_with_empty_typed_home_preserves_environment_fallback(
    tmp_path, monkeypatch
):
    fallback = tmp_path / "environment"
    monkeypatch.setenv("SONDER_HOME", str(fallback))

    bootstrap_app.build_application(config=SonderConfig())

    assert paths.state_path("jobs.db", "SONDER_JOBS_DB") == str(
        fallback / "jobs.db"
    )


def test_cmd_migrate_uses_typed_home_when_environment_is_poisoned(
    tmp_path, monkeypatch, capsys
):
    poisoned = tmp_path / "poisoned"
    configured = tmp_path / "configured"
    monkeypatch.setenv("SONDER_HOME", str(poisoned))
    seen: dict[str, str] = {}

    def capture_migrations(*, busy_timeout_ms=5000):
        del busy_timeout_ms
        seen["operations"] = migrations.store_db_paths()["operations"]
        return {}

    monkeypatch.setattr(migrations, "migrate_all", capture_migrations)

    assert main(["migrate", "--set", f"state.home={configured}"]) == 0
    capsys.readouterr()

    assert seen["operations"] == str(configured / "operations.db")
    assert not (poisoned / "operations.db").exists()


def test_cmd_serve_migrates_typed_home_before_binding_when_environment_is_poisoned(
    tmp_path, monkeypatch, capsys
):
    poisoned = tmp_path / "poisoned"
    configured = tmp_path / "configured"
    monkeypatch.setenv("SONDER_HOME", str(poisoned))
    seen: dict[str, str] = {}

    class SuccessfulPreflight:
        ok = True
        checks = ()

    def capture_migrations(*, busy_timeout_ms=5000):
        del busy_timeout_ms
        seen["jobs"] = migrations.store_db_paths()["jobs"]
        raise migrations.MigrationError("stop before listener")

    monkeypatch.setattr(
        "sonder_runtime.__main__._run_preflight",
        lambda *args, **kwargs: SuccessfulPreflight(),
    )
    monkeypatch.setattr(migrations, "migrate_all", capture_migrations)

    assert main(
        [
            "serve",
            "--skip-preflight",
            "--set",
            f"state.home={configured}",
        ]
    ) == 1
    capsys.readouterr()

    assert seen["jobs"] == str(configured / "jobs.db")
    assert not (poisoned / "jobs.db").exists()


def test_cmd_serve_refuses_pre_epoch_home_and_points_to_explicit_adoption(
    tmp_path, monkeypatch, capsys
):
    import sqlite3

    configured = tmp_path / "pre-epoch"
    configured.mkdir()
    conn = sqlite3.connect(str(configured / "memory.db"))
    conn.execute("CREATE TABLE schema_epoch (epoch INTEGER)")
    conn.execute("INSERT INTO schema_epoch VALUES (1)")
    conn.commit()
    conn.close()

    called = []
    monkeypatch.setattr(migrations, "migrate_all", lambda **kwargs: called.append(kwargs))
    assert main([
        "serve", "--skip-preflight", "--set", f"state.home={configured}",
    ]) == 1
    assert called == []
    assert "migrate --adopt-epoch2" in capsys.readouterr().err
