"""Focused evidence for the historical fleet-store import boundary."""

from __future__ import annotations

import importlib
import sqlite3
import tempfile
from pathlib import Path


def test_root_fleet_store_is_the_canonical_module_and_preserves_exports():
    legacy = importlib.import_module("fleet_store")
    canonical = importlib.import_module(
        "sonder_runtime.adapters.persistence.fleet_store"
    )

    assert legacy is canonical
    assert legacy._ensure_schema is canonical._ensure_schema
    public_names = {name for name in dir(canonical) if not name.startswith("_")}
    namespace = {}
    exec("from fleet_store import *", namespace)
    assert public_names <= set(namespace)


def test_fleet_baseline_can_still_import_legacy_store_and_apply():
    baseline = importlib.import_module("migrations.fleet.0001_baseline")
    with tempfile.TemporaryDirectory(
        prefix="fleet-store-compat-", dir=Path.cwd()
    ) as directory:
        database = Path(directory) / "fleet.db"
        connection = sqlite3.connect(database)
        try:
            baseline.apply(connection)
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            connection.close()

    assert "fleet_agents" in tables
    assert "fleet_events" in tables
