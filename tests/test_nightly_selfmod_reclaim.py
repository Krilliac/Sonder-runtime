"""reclaim_orphans() reclaims only runs the nightly loop itself created."""
from __future__ import annotations

import selfmod
from scripts import nightly_selfmod
from tests import test_selfmod as fixture_helpers
from tests.test_selfmod import repository

isolated = fixture_helpers.isolated


def _plan(root, created_by=""):
    return selfmod.create_plan(
        "fix deterministic addition defect", root,
        problem="add subtracts instead of adding",
        evidence=["tests/test_calc.py::test_add fails with -1 instead of 5"],
        files=["calc.py"],
        criteria=["reproducing test passes"],
        risk="low",
        created_by=created_by,
    )


def test_reclaim_skips_ownerless_operator_runs(isolated, monkeypatch):
    root = repository(isolated, use_git=False)
    selfmod.set_mode("propose")
    operator = _plan(root)
    selfmod.create_backup(operator["id"])
    loop = _plan(root, created_by=nightly_selfmod.LOOP_CREATOR)
    selfmod.create_backup(loop["id"])
    discarded = []
    monkeypatch.setattr(nightly_selfmod, "_discard_workspace", discarded.append)

    assert nightly_selfmod.reclaim_orphans(lambda _message: None) == 1

    assert discarded == [loop["id"]]
    assert selfmod.get_run(loop["id"])["phase"] == "cancelled"
    assert selfmod.get_run(operator["id"])["phase"] == "backed_up"


def test_created_by_is_persisted_and_defaults_to_unattributed(isolated):
    root = repository(isolated, use_git=False)
    selfmod.set_mode("propose")
    assert _plan(root)["created_by"] == ""
    assert _plan(root, created_by=nightly_selfmod.LOOP_CREATOR)["created_by"] == (
        nightly_selfmod.LOOP_CREATOR
    )
