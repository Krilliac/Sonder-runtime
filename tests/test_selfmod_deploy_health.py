"""The post-deploy health check must exercise rollback, not merely import.

Defect #46. ``server.py`` deployed with
``health_command=[sys.executable, "-c", "import server; print(server.status())"]``.
That proves the newly written bytes *import*. It never calls
``selfmod.rollback``, and it never touches ``selfmod_recover``.

Normally that is contained: ``selfmod.py`` and ``selfmod_recover.py`` are
``_protected()``, so an ordinary deploy cannot rewrite them. Under an
operator-typed ``--maintenance`` deploy both fit inside a single eight-file
run. A maintenance deploy that leaves ``selfmod.py`` importable but its restore
path broken therefore passed the health check, was marked ``deployed``, and
left the installation with no working rollback -- the exact state auto-restore
exists to prevent, reached through the mechanism meant to prevent it.

The recovery *architecture* is not at fault and is not revisited here.
``selfmod_recover.py`` imports no Sonder modules and restores from an
out-of-tree manifest; that design is sound. What was missing was a check that
the deployed bytes can still run it.

Nothing here deploys to the live installation. Every run uses a scratch
repository under ``tmp_path`` and an isolated ``SONDER_SELFMOD_HOME``. The
scratch repository's ``selfmod.py``/``selfmod_recover.py`` are byte copies of
this checkout's, so the probe exercises real restore machinery rather than a
toy stand-in; the supporting modules resolve from this checkout via
``PYTHONPATH``.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import selfmod

REPO_ROOT = Path(__file__).resolve().parent.parent

SERVER_STUB = '''"""Scratch stand-in for the module the old health command imported."""
import selfmod


def status():
    return {"ok": True, "note": getattr(selfmod, "SELFMOD_NOTE", "v1")}
'''

# The candidate's declared, legitimate objective: publish a version marker.
NOTE_MARKER = '\nSELFMOD_NOTE = "v2"\n'

# The rider that breaks rollback. `_restore_manifest_files` is what every
# rollback route funnels through -- `rollback` -> `restore` -> here. Naming an
# undefined helper keeps the module perfectly importable and breaks it the
# moment it is actually called, which is precisely the filed failure mode.
INTREE_GOOD = '    root = Path(manifest["repository_root"])\n    for record in manifest["files"]:\n        target = root / record["path"]\n        if record["existed_before"]:\n            _atomic_copy('
INTREE_BROKEN = '    root = Path(_manifest_root_for_restore(manifest))\n    for record in manifest["files"]:\n        target = root / record["path"]\n        if record["existed_before"]:\n            _atomic_copy('

# The out-of-tree half: same shape, applied to selfmod_recover.restore.
RECOVER_GOOD = '    root, validated = _validated_manifest(manifest_path, manifest)\n'
RECOVER_BROKEN = '    root = Path(_emergency_root(manifest)).resolve()\n'


def _reproducer():
    """Fails on the untouched source, passes once the marker lands."""
    return [
        sys.executable, "-c",
        "import sys; sys.path.insert(0, '.'); import selfmod; "
        "raise SystemExit(0 if getattr(selfmod, 'SELFMOD_NOTE', '') == 'v2' else 1)",
    ]


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=root, text=True, capture_output=True, check=True)


@pytest.fixture
def scratch(monkeypatch, tmp_path):
    state = tmp_path / "state"
    monkeypatch.setenv("SONDER_SELFMOD_HOME", str(state))
    monkeypatch.setenv("SONDER_SELFMOD_DB", str(state / "selfmod.db"))
    monkeypatch.delenv("SONDER_SELFMOD_ACTIVE", raising=False)
    # Supporting modules (sonder_paths, sonder_logging, sonder_runtime) resolve
    # from this checkout; selfmod/selfmod_recover resolve from the scratch root,
    # which is what makes the probe test the *deployed* bytes.
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))

    root = tmp_path / "repo"
    root.mkdir()
    shutil.copy2(REPO_ROOT / "selfmod.py", root / "selfmod.py")
    shutil.copy2(REPO_ROOT / "selfmod_recover.py", root / "selfmod_recover.py")
    (root / "server.py").write_text(SERVER_STUB, encoding="utf-8")
    _git(root, "init", "--initial-branch=main")
    _git(root, "config", "user.email", "selfmod@test.invalid")
    _git(root, "config", "user.name", "Selfmod Health Test")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "initial")
    selfmod.set_mode("propose")
    return root


def _approved_run(root, *, changes, files):
    """Drive a --maintenance run to `approved` with every pre-deploy check green."""
    run = selfmod.create_plan(
        "publish a selfmod version marker", root,
        problem="selfmod exposes no version marker",
        evidence=["the marker probe exits 1 against the untouched source"],
        files=list(files),
        criteria=["the marker probe passes after the edit"],
        expected_benefit="callers can read the marker",
        rollback_plan="restore immutable backup",
        maintenance_authorized=True,
    )
    run_id = run["id"]
    assert run["maintenance_authorized"] is True
    selfmod.create_backup(run_id)
    selfmod.prepare_workspace(run_id)
    selfmod.apply_candidate_changes(run_id, changes)
    selfmod.record_reproducer_before(run_id, _reproducer())
    selfmod.begin_testing(run_id)
    checks = {
        "syntax": [sys.executable, "-m", "py_compile", "selfmod.py", "selfmod_recover.py"],
        "targeted": _reproducer(),
        "regression": [sys.executable, "-c", "import sys; sys.path.insert(0, '.'); import server; print(server.status())"],
        "smoke": [sys.executable, "-c", "import sys; sys.path.insert(0, '.'); import selfmod_recover; print(selfmod_recover.restore)"],
        "security": [sys.executable, "-c", "print('no new network or credential surface')"],
    }
    for kind, command in checks.items():
        row = selfmod.record_test(run_id, kind, command)
        assert row["passed"], "pre-deploy %s check did not pass: %r" % (kind, row["output"][-400:])
    reviewed = selfmod.review(run_id)
    assert reviewed["phase"] == "reviewing", reviewed["last_error"]
    selfmod.approve(run_id, approver="explicit local/developer user")
    return run_id


def _mutate(source_path, old, new):
    text = source_path.read_text(encoding="utf-8")
    assert text.count(old) == 1, "anchor for the injected breakage is not unique in %s" % source_path
    return text.replace(old, new)


def _broken_intree_selfmod(root):
    return _mutate(root / "selfmod.py", INTREE_GOOD, INTREE_BROKEN) + NOTE_MARKER


def _sound_selfmod(root):
    return (root / "selfmod.py").read_text(encoding="utf-8") + NOTE_MARKER


def _old_health_command():
    """Verbatim shape of what server.py passed before this fix."""
    return [sys.executable, "-c", "import sys; sys.path.insert(0, '.'); import server; print(server.status())"]


def test_deploy_refuses_a_maintenance_run_whose_rollback_is_broken(scratch):
    """The reproduction, inverted into a requirement.

    The candidate is importable, its declared objective is met, and every
    pre-deploy check is green -- yet `_restore_manifest_files` raises the
    moment rollback needs it. Deploy must not finish on that.
    """
    run_id = _approved_run(scratch, changes={"selfmod.py": _broken_intree_selfmod(scratch)}, files=["selfmod.py"])
    with pytest.raises(RuntimeError) as caught:
        selfmod.deploy(run_id, health_command=_old_health_command(), commit=False)
    message = str(caught.value).lower()
    assert "rollback" in message, "the refusal must name what it caught, got: %s" % caught.value
    assert selfmod.get_run(run_id)["phase"] == "restored"


def test_deploy_verifies_rollback_when_no_health_command_is_supplied(scratch):
    """`scripts/nightly_selfmod.py` calls `selfmod.deploy(run_id)` bare.

    The unattended lane passes no health_command at all, so a check that only
    runs when a caller remembers to ask for one protects nothing there.
    """
    run_id = _approved_run(scratch, changes={"selfmod.py": _broken_intree_selfmod(scratch)}, files=["selfmod.py"])
    with pytest.raises(RuntimeError) as caught:
        selfmod.deploy(run_id, commit=False)
    assert "rollback" in str(caught.value).lower()
    assert selfmod.get_run(run_id)["phase"] == "restored"


def test_deploy_refuses_when_the_out_of_tree_recovery_entry_point_is_broken(scratch):
    """`selfmod_recover.py` is the last line of defence and is deployable too."""
    changes = {
        "selfmod.py": _sound_selfmod(scratch),
        "selfmod_recover.py": _mutate(scratch / "selfmod_recover.py", RECOVER_GOOD, RECOVER_BROKEN),
    }
    run_id = _approved_run(scratch, changes=changes, files=["selfmod.py", "selfmod_recover.py"])
    with pytest.raises(RuntimeError) as caught:
        selfmod.deploy(run_id, health_command=_old_health_command(), commit=False)
    assert "rollback" in str(caught.value).lower()
    assert selfmod.get_run(run_id)["phase"] == "restored"


def test_a_refused_deploy_leaves_the_original_bytes_in_place(scratch):
    """The check runs after the new bytes land, so failing must be recoverable."""
    original = (scratch / "selfmod.py").read_text(encoding="utf-8")
    run_id = _approved_run(scratch, changes={"selfmod.py": _broken_intree_selfmod(scratch)}, files=["selfmod.py"])
    with pytest.raises(RuntimeError):
        selfmod.deploy(run_id, health_command=_old_health_command(), commit=False)
    assert (scratch / "selfmod.py").read_text(encoding="utf-8") == original
    assert "_manifest_root_for_restore" not in (scratch / "selfmod.py").read_text(encoding="utf-8")
    strays = [p.name for p in scratch.iterdir() if "emergency-" in p.name or "selfmod-rollback" in p.name]
    assert strays == [], "the probe left scratch files in the repository root: %s" % strays


def test_a_sound_maintenance_deploy_records_a_passing_rollback_check(scratch):
    """The check has to be able to pass, or it is a different kind of useless."""
    run_id = _approved_run(scratch, changes={"selfmod.py": _sound_selfmod(scratch)}, files=["selfmod.py"])
    run = selfmod.deploy(run_id, health_command=_old_health_command(), commit=False)
    assert run["phase"] == "deployed"
    rows = [row for row in selfmod.test_results(run_id) if row["kind"] == "post_deploy_rollback"]
    assert len(rows) == 1, "expected exactly one recorded rollback verification, got %d" % len(rows)
    assert rows[0]["passed"] is True, rows[0]["output"][-800:]
    assert "SELFMOD-ROLLBACK-RECEIPT" in rows[0]["output"]
    assert (scratch / "selfmod.py").read_text(encoding="utf-8").endswith(NOTE_MARKER)
