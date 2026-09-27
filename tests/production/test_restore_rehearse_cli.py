"""``python -m sonder_runtime restore rehearse`` runs the P6 offline recovery
rehearsal against a real backup, end to end through ``main()``.

The rehearsal contract and its filesystem adapter already had focused
coverage (tests/test_offline_recovery_rehearsal.py) but no operator entry
point: nothing outside the tests constructed the adapter.  These tests drive
the CLI the way the runbook does -- create a backup, rehearse it -- and check
the evidence it prints, that the live state home is untouched, and that the
disposable tree is gone afterwards; and that a tampered or mismatched backup
exits nonzero before anything is staged.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from sonder_runtime.__main__ import main

pytestmark = pytest.mark.unit

FAILED_UPGRADE_STEPS = [
    "inspect_backup", "verify_manifest", "verify_artifacts", "stage_restore",
    "verify_restore", "apply_upgrade", "rollback_upgrade", "restore_state",
    "verify_rollback", "cleanup",
]


@pytest.fixture()
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("SONDER_HOME", str(home))
    monkeypatch.setenv("SONDER_OPERATIONS_DB", str(home / "operations.db"))
    monkeypatch.setenv("SONDER_DB", str(home / "memory.db"))
    monkeypatch.setenv("SONDER_AUTOPILOT_DB", str(home / "autopilot.db"))
    monkeypatch.setenv("SONDER_FLEET_DB", str(home / "fleet.db"))
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(home / "runtime_policy.json"))
    return home


@pytest.fixture()
def backup_dir(isolated_home, tmp_path, capsys) -> Path:
    assert main(["migrate", "--store", "operations"]) == 0
    capsys.readouterr()
    target = tmp_path / "backups"
    assert main(["backup", "create", "--target", str(target), "--json"]) == 0
    return Path(json.loads(capsys.readouterr().out)["path"])


def _tree_digest(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*")) if path.is_file()
    }


def test_rehearse_runs_every_step_and_leaves_live_state_alone(
    isolated_home, backup_dir, tmp_path, capsys,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    live_before = _tree_digest(isolated_home)
    backup_before = _tree_digest(backup_dir)

    rc = main(["restore", "rehearse", str(backup_dir), "--workspace", str(workspace), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0, payload
    assert payload["ok"] is True
    assert payload["steps"] == FAILED_UPGRADE_STEPS
    assert payload["rollback_verified"] is True
    assert payload["upgrade_succeeded"] is False
    assert payload["live_failover"] is False
    assert payload["target_revision"] == payload["source_revision"] + "+rehearsal"
    assert payload["manifest_sha256"] == hashlib.sha256(
        (backup_dir / "manifest.json").read_bytes()).hexdigest()
    assert payload["checksum_sha256"] == hashlib.sha256(
        (backup_dir / "checksums.sha256").read_bytes()).hexdigest()
    assert len(payload["restore_digest"]) == 64
    assert len(payload["evidence_digest"]) == 64
    assert payload["cleanup"]["remaining_entries"] == 0
    assert payload["cleanup"]["removed_entries"] >= 1
    assert payload["cleanup"]["bounded"] is True
    assert list(workspace.iterdir()) == [], "the disposable tree was removed"
    assert _tree_digest(isolated_home) == live_before, "live state home was touched"
    assert _tree_digest(backup_dir) == backup_before, "the backup was modified"


def test_rehearse_without_workspace_uses_and_removes_a_fresh_one(backup_dir, capsys):
    rc = main(["restore", "rehearse", str(backup_dir), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0, payload
    assert payload["steps"] == FAILED_UPGRADE_STEPS
    assert not Path(payload["workspace"]).exists()


def test_rehearse_text_output_names_the_evidence(backup_dir, tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    assert main(["restore", "rehearse", str(backup_dir), "--workspace", str(workspace)]) == 0
    out = capsys.readouterr().out
    assert "manifest_digest:" in out
    assert "restore_digest:" in out
    assert "rollback_verified: True" in out
    assert "Offline recovery rehearsal passed" in out


def test_a_tampered_backup_fails_before_any_staging_write(backup_dir, tmp_path, capsys):
    member = next(path for path in sorted((backup_dir / "state").iterdir()) if path.is_file())
    member.write_bytes(member.read_bytes() + b"tampered")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    rc = main(["restore", "rehearse", str(backup_dir), "--workspace", str(workspace), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 1
    assert payload["ok"] is False
    assert payload["error"] == "ArtifactIntegrityError"
    assert "checksum" in payload["message"]
    assert "stage_restore" not in payload["steps_completed"]
    assert payload["destination_left"] is False
    assert list(workspace.iterdir()) == []


def test_a_revision_mismatch_fails_before_any_staging_write(backup_dir, tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    rc = main([
        "restore", "rehearse", str(backup_dir), "--workspace", str(workspace),
        "--source-revision", "not-the-recorded-revision", "--json",
    ])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["error"] == "RevisionMismatchError"
    assert payload["steps_completed"] == ["inspect_backup"]
    assert list(workspace.iterdir()) == []


def test_a_missing_backup_is_reported_not_raised(tmp_path, capsys):
    rc = main(["restore", "rehearse", str(tmp_path / "absent"), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["ok"] is False
    assert payload["steps_completed"] == []
    assert not Path(payload["workspace"]).exists()


def test_an_identical_target_revision_is_a_usage_error(backup_dir, tmp_path, capsys):
    rc = main([
        "restore", "rehearse", str(backup_dir), "--source-revision", "rev-a",
        "--target-revision", "rev-a", "--json",
    ])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert payload["error"] == "request_invalid"


def test_a_symlinked_workspace_is_refused(backup_dir, tmp_path, capsys):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    rc = main(["restore", "rehearse", str(backup_dir), "--workspace", str(link), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert payload["error"] == "workspace_invalid"
    assert list(real.iterdir()) == []
