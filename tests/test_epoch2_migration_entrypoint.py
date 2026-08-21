from __future__ import annotations

import json

from sonder_runtime.__main__ import main
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


def test_epoch2_migration_is_explicit_and_does_not_change_normal_migrate(
    tmp_path, capsys
):
    try:
        assert main([
            "migrate", "--json", "--set", f"state.home={tmp_path}",
        ]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert "adopted" not in payload
        assert not (tmp_path / "epoch2_adoption_receipt.json").exists()
    finally:
        runtime_paths.reset_home()
