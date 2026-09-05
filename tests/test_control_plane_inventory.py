from pathlib import Path
import pytest
from sonder_runtime.platform import paths
from sonder_runtime.adapters.filesystem import file_ops


@pytest.fixture
def state(tmp_path, monkeypatch):
    for name in (
        "SONDER_FLEET_DB",
        "SONDER_APPROVALS_DB",
        "SONDER_DB",
        "SONDER_SESSIONS_DB",
        "SONDER_JOBS_DB",
        "SONDER_CHILD_SESSIONS_DB",
        "SONDER_TOOL_AUDIT",
        "SONDER_LANE_TEST_TARGETS_FILE",
        "SONDER_FANOUT_DB",
        "SONDER_EXTENSIONS_DB",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(paths, "_configured_home", lambda: None)
    monkeypatch.setenv("SONDER_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.mark.parametrize(
    "variable",
    [
        "SONDER_FLEET_DB",
        "SONDER_APPROVALS_DB",
        "SONDER_DB",
        "SONDER_SESSIONS_DB",
        "SONDER_JOBS_DB",
        "SONDER_CHILD_SESSIONS_DB",
    ],
)
def test_actual_relative_overrides_and_sqlite_sidecars_protected(
    state, monkeypatch, variable
):
    from sonder_runtime.adapters.security.control_plane_paths import (
        live_control_plane_inventory,
    )

    monkeypatch.setenv(variable, "relocated/private.sqlite")
    inventory = live_control_plane_inventory()
    for suffix in ("", "-wal", "-shm", "-journal"):
        target = state / ("relocated/private.sqlite" + suffix)
        assert inventory.protects(target)
        assert file_ops._is_sensitive_control_path(target)
        with pytest.raises(PermissionError):
            file_ops.write_file(str(target), "tamper", bypass=True)
    assert not inventory.protects(state / "relocated/ordinary.txt")


def test_fleet_owner_locks_audit_rotations_and_catalog(state, monkeypatch):
    from sonder_runtime.adapters.security.control_plane_paths import (
        live_control_plane_inventory,
    )

    monkeypatch.setenv("SONDER_FLEET_DB", "fleet/custom.db")
    monkeypatch.setenv("SONDER_TOOL_AUDIT", "audit/custom.jsonl")
    monkeypatch.setenv("SONDER_LANE_TEST_TARGETS_FILE", "catalog/targets.json")
    inventory = live_control_plane_inventory()
    for name in (
        "fleet/lane-owner-" + "a" * 32 + ".lock",
        "audit/custom.20260904.1.jsonl",
        "catalog/targets.json",
    ):
        assert inventory.protects(state / name)
    assert not inventory.protects(state / "fleet/readme.txt")
    with pytest.raises(PermissionError):
        inventory.require_disjoint((state / "fleet",))


def test_additional_owned_store_scope_restores_default_on_exception(state):
    from sonder_runtime.adapters.security.control_plane_paths import (
        ControlPlanePaths,
        control_plane_scope,
    )

    owned = state / "output"
    extra = ControlPlanePaths(owned_directories=(owned,))
    with pytest.raises(RuntimeError):
        with control_plane_scope(extra):
            assert file_ops._is_sensitive_control_path(owned / "arbitrary.tmp")
            assert file_ops._is_sensitive_control_path(state / "state/fleet.db")
            raise RuntimeError("scope exit")
    assert not file_ops._is_sensitive_control_path(owned / "arbitrary.tmp")
    assert file_ops._is_sensitive_control_path(state / "state/fleet.db")


def test_inventory_is_readonly_bounded_and_configured_home_precedence(
    state, monkeypatch
):
    from sonder_runtime.adapters.security.control_plane_paths import (
        live_control_plane_inventory,
        ControlPlanePaths,
    )

    configured = state / "configured"
    monkeypatch.setattr(paths, "_configured_home", lambda: configured)
    monkeypatch.setenv("SONDER_FLEET_DB", "ignored.db")
    monkeypatch.setenv("SONDER_SESSIONS_DB", "explicit-session.db")
    inventory = live_control_plane_inventory()
    assert inventory.protects(configured / "fleet.db")
    assert inventory.protects(state / "explicit-session.db")
    assert not configured.exists()
    with pytest.raises(ValueError):
        ControlPlanePaths(files=tuple(state / str(index) for index in range(257)))


def test_relocated_database_rejected_by_actual_sqlite_mutation_tool(state, monkeypatch):
    import sqlite3
    import sqlite_mutate as mutate

    database = state / "private.db"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE records(value INTEGER)")
        conn.execute("INSERT INTO records VALUES(1)")
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(state))
    monkeypatch.setenv("SONDER_APPROVALS_DB", str(database))
    with pytest.raises(mutate.SqliteMutateError, match="control"):
        mutate.mutate_sqlite(database, "UPDATE records SET value=?", [2])
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT value FROM records").fetchone() == (1,)


def test_live_additional_provider_and_context_scope_are_isolated(state):
    from contextvars import Context
    from sonder_runtime.adapters.security.control_plane_paths import (
        ControlPlanePaths,
        control_plane_scope,
        live_control_plane_inventory,
    )

    current = [ControlPlanePaths(owned_directories=(state / "output",))]
    first = live_control_plane_inventory(additional=lambda: current[0])
    current[0] = ControlPlanePaths(owned_directories=(state / "replacement",))
    second = live_control_plane_inventory(additional=lambda: current[0])
    assert first.protects(state / "output/a")
    assert second.protects(state / "replacement/a")
    with control_plane_scope(current[0]):
        assert file_ops._is_sensitive_control_path(state / "replacement/a")
        assert not Context().run(
            file_ops._is_sensitive_control_path, state / "replacement/a"
        )
    with pytest.raises(TypeError):
        live_control_plane_inventory(additional=lambda: {"files": []})
