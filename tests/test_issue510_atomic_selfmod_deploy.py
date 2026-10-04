"""Original #510: supported checkout promotion has one atomic file boundary."""
from __future__ import annotations

import sys
import subprocess
import time
from pathlib import Path

import pytest

import selfmod
from sonder_runtime.bootstrap.app import build_application
from sonder_runtime.platform.config import SonderConfig, StateConfig
from tests import test_selfmod as fixture_helpers
from tests.test_selfmod import hashes, repository, reviewed

isolated = fixture_helpers.isolated


@pytest.mark.parametrize("use_git", [False, True])
def test_multifile_checkout_promotion_refuses_before_any_live_copy(isolated, monkeypatch, use_git):
    stages = build_application(config=SonderConfig(
        state=StateConfig(home=str(isolated / "journal-home")),
    )).selfmod_service()
    root = repository(isolated, use_git=use_git)
    run = reviewed(root, files=("calc.py", "tests/test_new.py"))
    selfmod.approve(run["id"], "user:test")
    live_before = hashes(root)
    candidate_before = hashes(selfmod.candidate_path(run["id"]))
    manifest_before = selfmod.verify_backup(run["id"])

    def no_live_copy(*args, **kwargs):
        pytest.fail("multi-file promotion reached the live-copy boundary")

    monkeypatch.setattr(selfmod, "_atomic_copy", no_live_copy)
    with pytest.raises(selfmod.SelfmodStageNotApplied, match="exactly one changed file"):
        selfmod.deploy(run["id"], commit=use_git)
    assert hashes(root) == live_before
    assert hashes(selfmod.candidate_path(run["id"])) == candidate_before
    assert selfmod.verify_backup(run["id"]) == manifest_before
    assert selfmod.get_run(run["id"])["phase"] == "approved"
    assert not (root / "tests/test_new.py").exists()
    for _ in range(2):
        with pytest.raises(selfmod.SelfmodStageNotApplied, match="exactly one changed file"):
            stages.journaled_stage(run["id"], "deploy", {"commit": use_git},
                                   lambda: selfmod.deploy(run["id"], commit=use_git))
    binding = stages._effect_binding_factory(run["id"])
    effects = binding.journal.effects_since(binding.run_id, 0, limit=10).records
    assert len(effects) == 2
    assert all(effect.state.value == "failed" and effect.receipt_key.endswith(":not-applied")
               for effect in effects)
    assert hashes(root) == live_before


@pytest.mark.parametrize("use_git", [False, True])
def test_singlefile_checkout_promotion_and_rollback_preserve_exact_bytes(isolated, use_git):
    root = repository(isolated, use_git=use_git)
    live_before = hashes(root)
    run = reviewed(root)
    manifest = selfmod.verify_backup(run["id"])
    assert [record["path"] for record in manifest["files"]] == ["calc.py"]
    selfmod.approve(run["id"], "user:test")
    deployed = selfmod.deploy(
        run["id"], commit=use_git,
        health_command=[sys.executable, "-c", "from calc import add; assert add(10,2)==12"],
    )
    assert deployed["phase"] == "deployed"
    assert (root / "calc.py").read_text(encoding="utf-8") == "def add(a, b):\n    return a + b\n"
    assert selfmod.rollback(run["id"])["phase"] == "restored"
    assert hashes(root) == live_before


@pytest.mark.parametrize("use_git", [False, True])
@pytest.mark.parametrize("restart", [False, True])
def test_rollback_restores_only_deployed_file_and_preserves_unchanged_scope_edits(isolated, use_git, restart):
    root = repository(isolated, use_git=use_git)
    original = (root / "calc.py").read_bytes()
    run = reviewed(root, files=("calc.py", "tests/test_calc.py"))
    selfmod.approve(run["id"], "user:test")
    selfmod.deploy(run["id"], commit=use_git)
    sibling = root / "tests/test_calc.py"
    annotation = sibling.read_text(encoding="utf-8") + "\n# Subsequent user annotation\n"
    sibling.write_text(annotation, encoding="utf-8")
    if use_git:
        fixture_helpers.git(root, "add", "tests/test_calc.py")
    if restart:
        subprocess.run([
            sys.executable, "-c", "import sys; sys.path.insert(0, %r); import selfmod; selfmod.rollback(%r)"
            % (str(Path(selfmod.__file__).parent), run["id"]),
        ], cwd=root, capture_output=True, text=True, check=True, timeout=15)
    else:
        selfmod.rollback(run["id"])
    assert (root / "calc.py").read_bytes() == original
    assert sibling.read_text(encoding="utf-8") == annotation
    assert selfmod.get_run(run["id"])["phase"] == "restored"
    if use_git:
        assert fixture_helpers.git(root, "diff", "--cached", "--name-only").stdout.strip() == "tests/test_calc.py"
        assert fixture_helpers.git(root, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").stdout.strip() == "calc.py"


def test_restart_recovery_uses_tested_write_set_before_inventory_publication(isolated):
    root = repository(isolated, use_git=False)
    original = (root / "calc.py").read_bytes()
    run = reviewed(root, files=("calc.py", "tests/test_calc.py"))
    selfmod.approve(run["id"], "user:test")
    (root / "calc.py").write_bytes((selfmod.candidate_path(run["id"]) / "calc.py").read_bytes())
    sibling = root / "tests/test_calc.py"
    annotation = sibling.read_text(encoding="utf-8") + "\n# Subsequent user annotation\n"
    sibling.write_text(annotation, encoding="utf-8")
    with selfmod._tx() as conn:
        conn.execute(
            "UPDATE selfmod_deployment_lock SET owner_id='crashed',owner_pid=99999999,owner_host=?,lease_until=?,run_id=? WHERE id=1",
            (selfmod.socket.gethostname(), time.time() - 1, run["id"]),
        )
    subprocess.run([
        sys.executable, "-c", "import sys; sys.path.insert(0, %r); import selfmod; assert selfmod.reconcile_stale_deployment() == 1"
        % str(Path(selfmod.__file__).parent),
    ], cwd=root, capture_output=True, text=True, check=True, timeout=15)
    assert (root / "calc.py").read_bytes() == original
    assert sibling.read_text(encoding="utf-8") == annotation
    assert selfmod.get_run(run["id"])["phase"] == "restored"


def test_deployment_refuses_unrelated_staged_user_edit_before_live_writes(isolated):
    root = repository(isolated, use_git=True)
    run = reviewed(root)
    selfmod.approve(run["id"], "user:test")
    sibling = root / "tests/test_calc.py"
    annotation = sibling.read_text(encoding="utf-8") + "\n# Subsequent user annotation\n"
    sibling.write_text(annotation, encoding="utf-8")
    fixture_helpers.git(root, "add", "tests/test_calc.py")
    with pytest.raises(selfmod.SelfmodStageNotApplied, match="source tree changed"):
        selfmod.deploy(run["id"])
    assert (root / "calc.py").read_text(encoding="utf-8") == "def add(a, b):\n    return a - b\n"
    assert sibling.read_text(encoding="utf-8") == annotation
    assert fixture_helpers.git(root, "diff", "--cached", "--name-only").stdout.strip() == "tests/test_calc.py"


def test_deployment_commit_preserves_user_edit_staged_after_admission(isolated, monkeypatch):
    root = repository(isolated, use_git=True)
    run = reviewed(root)
    selfmod.approve(run["id"], "user:test")
    sibling = root / "tests/test_calc.py"
    annotation = sibling.read_text(encoding="utf-8") + "\n# Subsequent user annotation\n"
    original_copy = selfmod._atomic_copy

    def copy_then_stage_user_edit(source, target, mode):
        original_copy(source, target, mode)
        sibling.write_text(annotation, encoding="utf-8")
        fixture_helpers.git(root, "add", "tests/test_calc.py")

    monkeypatch.setattr(selfmod, "_atomic_copy", copy_then_stage_user_edit)
    assert selfmod.deploy(run["id"])["phase"] == "deployed"
    assert sibling.read_text(encoding="utf-8") == annotation
    assert fixture_helpers.git(root, "diff", "--cached", "--name-only").stdout.strip() == "tests/test_calc.py"
    assert fixture_helpers.git(root, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").stdout.strip() == "calc.py"
