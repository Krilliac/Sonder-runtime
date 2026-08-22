from __future__ import annotations

import sonder_paths

from sonder_runtime.adapters.persistence import migrations


def test_operations_db_path_uses_packaged_paths_boundary(monkeypatch):
    calls: list[tuple[str, str]] = []

    def fake_state_path(filename: str, env_var: str) -> str:
        calls.append((filename, env_var))
        return "C:/isolated/operations.db"

    monkeypatch.setattr(migrations.platform_paths, "state_path", fake_state_path)

    assert migrations._operations_db_path() == "C:/isolated/operations.db"
    assert calls == [("operations.db", "SONDER_OPERATIONS_DB")]


def test_migration_registry_keeps_immutable_root_and_store_locations(monkeypatch):
    original_root = migrations.MIGRATIONS_ROOT
    original_paths = migrations.store_db_paths

    monkeypatch.setattr(migrations.platform_paths, "state_path", lambda name, env: f"/state/{name}")

    paths = original_paths()

    assert migrations.MIGRATIONS_ROOT == original_root
    assert paths["operations"] == "/state/operations.db"
    assert paths["memory"].endswith("memory.db")


def test_all_migration_path_calls_use_identity_preserving_package_seam():
    """The package seam must remain a byte/behavior-identical path bridge."""
    assert migrations.platform_paths.memory_db_path is sonder_paths.memory_db_path
    assert migrations.platform_paths.state_path is sonder_paths.state_path
    assert migrations.platform_paths.ensure_home is sonder_paths.ensure_home
