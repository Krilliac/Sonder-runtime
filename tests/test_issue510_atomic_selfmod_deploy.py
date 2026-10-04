"""Original #510: supported checkout promotion has one atomic file boundary."""
from __future__ import annotations

import sys

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
