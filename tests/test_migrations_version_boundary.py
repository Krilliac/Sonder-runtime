from __future__ import annotations

import hashlib
import sqlite3

from sonder_runtime.adapters.persistence import migrations
from sonder_runtime.platform import version


def test_migrations_consumes_the_packaged_version_boundary():
    import sonder_version

    assert migrations.platform_version is version
    assert migrations.platform_version.VERSION is sonder_version.VERSION
    assert migrations.platform_version.BuildInfo is sonder_version.BuildInfo
    assert migrations.platform_version.build_info is sonder_version.build_info


def test_migration_replay_preserves_immutable_bytes_checksums_and_release_metadata(
    tmp_path, monkeypatch
):
    expected = version.BuildInfo(
        version="9.8.7",
        commit_sha="a" * 40,
        stamped=True,
    )
    monkeypatch.setattr(version, "build_info", lambda: expected)

    immutable_bytes = {
        migration.path: migration.path.read_bytes()
        for migration in migrations.discover_migrations("operations")
    }
    expected_checksums = {
        migration.migration_id: hashlib.sha256(data).hexdigest()
        for path, data in immutable_bytes.items()
        for migration in migrations.discover_migrations("operations")
        if migration.path == path
    }
    db = str(tmp_path / "operations.db")

    first = migrations.migrate_store("operations", db)
    second = migrations.migrate_store("operations", db)

    assert first.applied == second.applied == ("0001_baseline",)
    assert second.pending == ()
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT migration_id, application_version, checksum_sha256 "
            "FROM schema_migrations"
        ).fetchone()
    assert row == (
        "0001_baseline",
        expected.version,
        expected_checksums["0001_baseline"],
    )
    assert {
        path: path.read_bytes() for path in immutable_bytes
    } == immutable_bytes
