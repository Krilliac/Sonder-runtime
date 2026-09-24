import os
from types import SimpleNamespace

import pytest

import server
from sonder_runtime.adapters.unit_of_work import UnitOfWorkAdapter
from sonder_runtime.application.ports.runtime_checkpoints import CheckpointError
from sonder_runtime.bootstrap.strategy import (
    compose_strategy_trace,
    configured_strategy_trace,
)


def test_stable_private_seal_key_restores_checkpoint_after_restart(tmp_path):
    db = tmp_path / "observations.db"
    key = tmp_path / "private" / "checkpoint.key"
    first = compose_strategy_trace(db_path=db, key_path=key)
    assert first.history("unknown") == ()
    value = key.read_bytes()
    assert len(value) == 32
    if os.name == "posix":
        assert key.stat().st_mode & 0o077 == 0
        assert key.parent.stat().st_mode & 0o077 == 0
    second = compose_strategy_trace(db_path=db, key_path=key)
    assert key.read_bytes() == value
    assert second.history("unknown") == ()


def test_existing_invalid_key_fails_closed_without_regeneration(tmp_path):
    db = tmp_path / "observations.db"
    key = tmp_path / "private" / "checkpoint.key"
    key.parent.mkdir(mode=0o700)
    key.write_bytes(b"bad")
    if os.name == "posix":
        key.chmod(0o600)
    with pytest.raises(CheckpointError, match="malformed"):
        compose_strategy_trace(db_path=db, key_path=key)
    assert key.read_bytes() == b"bad"


def test_existing_checkpoint_database_with_missing_key_fails_closed(tmp_path):
    db = tmp_path / "observations.db"
    key = tmp_path / "private" / "checkpoint.key"
    db.write_bytes(b"existing database")

    with pytest.raises(CheckpointError, match="without its seal key"):
        compose_strategy_trace(db_path=db, key_path=key)

    assert not key.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory ownership checks")
def test_private_key_directory_symlink_is_rejected(tmp_path):
    target = tmp_path / "actual-private"
    target.mkdir(mode=0o700)
    alias = tmp_path / "private"
    alias.symlink_to(target, target_is_directory=True)
    with pytest.raises(CheckpointError, match="directory is not private"):
        compose_strategy_trace(
            db_path=tmp_path / "observations.db",
            key_path=alias / "checkpoint.key",
        )


def test_strategy_observer_is_disabled_without_host_rollout(monkeypatch):
    monkeypatch.delenv("SONDER_STRATEGY_OBSERVE", raising=False)
    assert configured_strategy_trace() is None
    monkeypatch.setenv("SONDER_STRATEGY_OBSERVE", "unexpected")
    with pytest.raises(ValueError):
        configured_strategy_trace()


def test_autopilot_server_composes_observer_at_production_entry(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SONDER_STRATEGY_OBSERVE", "1")
    monkeypatch.setattr(
        server, "_application",
        lambda: SimpleNamespace(
            unit_of_work=lambda: UnitOfWorkAdapter(str(tmp_path / "memory.db")),
        ),
    )
    received = []

    def execute_run(run_id, owner_id, **options):
        received.append((options["strategy_trace"], options["strategy_memory"]))
        return {"id": run_id, "status": "paused"}

    monkeypatch.setattr(server.autopilot_controller, "execute_run", execute_run)
    result = server._execute_autopilot("auto-test", plan_only=True)

    assert result["status"] == "paused"
    assert len(received) == 1
    assert received[0][0] is not None
    assert received[0][0].history("auto-test") == ()
    assert received[0][1] is not None
