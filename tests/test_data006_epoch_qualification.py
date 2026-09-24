"""DATA-006 production epoch admission and future-schema refusal."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from sonder_runtime.adapters.persistence.migrations import FutureSchemaError
from sonder_runtime.adapters.persistence.sqlite.bridge_migration import (
    EPOCH2_DATABASES,
    require_epoch_2,
    run_bridge_migration,
)
from sonder_runtime.adapters.updates.service import UpdateRepository
from sonder_runtime.domain.common.errors import MigrationRequired


def _file_snapshot(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.name.endswith(("-shm", "-wal"))
    }


def _fresh_epoch2_home(root: Path) -> None:
    run_bridge_migration(root)
    assert all((root / name).is_file() for name in EPOCH2_DATABASES)
    require_epoch_2(root)


def test_supported_epoch2_catalog_opens_without_mutation(tmp_path: Path):
    _fresh_epoch2_home(tmp_path)
    before = _file_snapshot(tmp_path)
    require_epoch_2(tmp_path)
    assert _file_snapshot(tmp_path) == before


@pytest.mark.parametrize("database_name", EPOCH2_DATABASES)
def test_future_epoch_refuses_before_any_migration_or_write(tmp_path: Path, database_name: str):
    _fresh_epoch2_home(tmp_path)
    database = tmp_path / database_name
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE schema_epoch SET epoch=3")
    before = _file_snapshot(tmp_path)
    with pytest.raises(MigrationRequired, match="schema epoch"):
        require_epoch_2(tmp_path)
    assert _file_snapshot(tmp_path) == before


def test_updates_repository_reopens_supported_state_and_refuses_future_migration(tmp_path: Path):
    database = tmp_path / "updates.db"
    first = UpdateRepository(str(database))
    plan = first.create_plan(
        channel="stable", source_kind="offline", source_ref="bundle",
        from_release_id="prior", target_version="2.0", target_manifest_sha256="a" * 64,
        status="planned", idempotency_key="qualification",
    )
    reopened = UpdateRepository(str(database))
    assert reopened.get_plan(plan["update_id"])["target_version"] == "2.0"

    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO schema_migrations VALUES (?,?,?,?,?)",
            ("9999_future_schema", "2026-09-24T00:00:00Z", "future", "f" * 64, 0),
        )
    before = _file_snapshot(tmp_path)
    with pytest.raises(FutureSchemaError, match="unknown"):
        UpdateRepository(str(database))
    assert _file_snapshot(tmp_path) == before
