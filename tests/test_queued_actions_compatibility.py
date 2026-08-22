from __future__ import annotations

import importlib
import sqlite3


def test_root_queued_actions_name_redirects_to_canonical_module():
    root = importlib.import_module("queued_actions")
    canonical = importlib.import_module(
        "sonder_runtime.adapters.persistence.queued_actions"
    )

    assert root is canonical
    assert root.__name__ == canonical.__name__
    assert root._SCHEMA == canonical._SCHEMA


def test_queued_actions_baseline_can_consume_redirected_schema():
    baseline = importlib.import_module("migrations.queued_actions.0001_baseline")
    connection = sqlite3.connect(":memory:")
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

    assert {"queued_actions", "queued_action_transitions"} <= tables
