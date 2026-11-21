from __future__ import annotations

import json
import sqlite3

from sonder_runtime.__main__ import main
from sonder_runtime.adapters.persistence.sqlite.bridge_migration import (
    EPOCH2_DATABASES,
    check_epoch,
)
from sonder_runtime.platform import paths as runtime_paths


def test_epoch2_migration_entrypoint_runs_bridge_and_verifies(tmp_path, capsys):
    try:
        assert main([
            "migrate", "--adopt-epoch2", "--json",
            "--set", f"state.home={tmp_path}",
        ]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["adopted"] is True
        assert payload["epoch"] == 2
        assert payload["verified"] is True
        assert (tmp_path / "epoch2_adoption_receipt.json").is_file()
    finally:
        runtime_paths.reset_home()


def test_plain_migrate_on_fresh_home_stamps_it_as_fresh_install(tmp_path, capsys):
    try:
        assert main([
            "migrate", "--json", "--set", f"state.home={tmp_path}",
        ]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert "adopted" not in payload
        receipt = json.loads(
            (tmp_path / "epoch2_adoption_receipt.json").read_text(encoding="utf-8")
        )
        assert receipt["source_version"] == "fresh-install"
        for name in EPOCH2_DATABASES:
            assert check_epoch(tmp_path / name) == 2, name
    finally:
        runtime_paths.reset_home()


def test_plain_migrate_on_legacy_home_neither_adopts_nor_stamps(tmp_path, capsys):
    legacy = tmp_path / "memory.db"
    conn = sqlite3.connect(legacy)
    conn.execute("CREATE TABLE legacy_rows (id INTEGER)")
    conn.commit()
    conn.close()
    try:
        assert main([
            "migrate", "--json", "--set", f"state.home={tmp_path}",
        ]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert "adopted" not in payload
        assert not (tmp_path / "epoch2_adoption_receipt.json").exists()
        for name in EPOCH2_DATABASES:
            assert check_epoch(tmp_path / name) is None, name
    finally:
        runtime_paths.reset_home()
