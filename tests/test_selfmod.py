import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

import selfmod
import selfmod_recover


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, text=True, capture_output=True, check=True)


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    state = tmp_path / "state"
    monkeypatch.setenv("SONDER_SELFMOD_HOME", str(state))
    monkeypatch.setenv("SONDER_SELFMOD_DB", str(state / "selfmod.db"))
    monkeypatch.delenv("SONDER_SELFMOD_ACTIVE", raising=False)
    return tmp_path


def repository(tmp_path, *, use_git=True):
    root = tmp_path / ("repo-git" if use_git else "repo-snapshot")
    root.mkdir(parents=True)
    (root / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_calc.py").write_text(
        "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n", encoding="utf-8"
    )
    if use_git and shutil.which("git"):
        git(root, "init", "--initial-branch=main")
        git(root, "config", "user.email", "selfmod@test.invalid")
        git(root, "config", "user.name", "Selfmod Test")
        git(root, "add", ".")
        git(root, "commit", "-m", "initial")
    return root


def plan(root, files=("calc.py",), risk="low", maintenance=False):
    return selfmod.create_plan(
        "fix deterministic addition defect", root,
        problem="add subtracts instead of adding",
        evidence=["tests/test_calc.py::test_add fails with -1 instead of 5"],
        files=list(files),
        criteria=["reproducing test passes", "regression suite passes"],
        risk=risk,
        expected_benefit="correct arithmetic",
        rollback_plan="restore exact hashes",
        maintenance_authorized=maintenance,
    )


def prepare(run):
    selfmod.create_backup(run["id"])
    return selfmod.prepare_workspace(run["id"])


def validate(run_id, targeted=None):
    reproducer = targeted or [sys.executable, "-m", "pytest", "-q", "tests/test_calc.py"]
    selfmod.record_reproducer_before(run_id, reproducer)
    selfmod.begin_testing(run_id)
    commands = {
        "syntax": [sys.executable, "-m", "py_compile", "calc.py"],
        "targeted": reproducer,
        "regression": [sys.executable, "-m", "pytest", "-q"],
        "smoke": [sys.executable, "-c", "from calc import add; assert add(1,2)==3"],
    }
    return [selfmod.record_test(run_id, kind, command) for kind, command in commands.items()]


def reviewed(root, files=("calc.py",), mode="propose"):
    selfmod.set_mode(mode)
    run = prepare(plan(root, files))
    changes = {"calc.py": "def add(a, b):\n    return a + b\n"}
    if "tests/test_new.py" in files:
        changes["tests/test_new.py"] = "from calc import add\n\ndef test_new(): assert add(4, 5) == 9\n"
    selfmod.apply_candidate_changes(run["id"], changes)
    assert all(row["passed"] for row in validate(run["id"]))
    return selfmod.review(run["id"])


def hashes(root):
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
        and ".git" not in path.parts
        and "__pycache__" not in path.parts
        and ".pytest_cache" not in path.parts
    }


def test_backup_creation_and_hash_verification(isolated):
    root = repository(isolated)
    run = plan(root)
    backed = selfmod.create_backup(run["id"])
    manifest = selfmod.verify_backup(run["id"])
    record = manifest["files"][0]
    assert backed["phase"] == "backed_up"
    assert Path(backed["backup_manifest"]).is_file()
    assert record["sha256_before"] == record["sha256_backup"] == hashlib.sha256((root / "calc.py").read_bytes()).hexdigest()
    assert Path(record["backup_path"]).resolve().is_relative_to(selfmod.backups_root().resolve())


def test_observe_mode_cannot_enter_editing_lifecycle(isolated):
    root = repository(isolated, use_git=False)
    selfmod.set_mode("observe")
    run = plan(root)
    assert run["phase"] == "observed"
    with pytest.raises(RuntimeError, match="proposed"):
        selfmod.create_backup(run["id"])


def test_existing_new_and_deleted_files_restore_atomically(isolated):
    root = repository(isolated, use_git=False)
    original = hashes(root)
    run = prepare(plan(root, files=("calc.py", "new.py", "tests/test_calc.py")))
    selfmod.apply_candidate_changes(run["id"], {
        "calc.py": "broken\n", "new.py": "created\n", "tests/test_calc.py": None,
    })
    selfmod.begin_testing(run["id"])
    selfmod.reject(run["id"], "fixture rejection")
    assert hashes(root) == original
    assert not (root / "new.py").exists()
    assert (root / "tests/test_calc.py").is_file()


def test_candidate_rejection_recovers_unexpected_live_source_change(isolated):
    root = repository(isolated, use_git=False)
    original = (root / "calc.py").read_bytes()
    run = prepare(plan(root))

    # Defense in depth for a broken editing integration: rejection must not
    # retain an accidental write to a backed-up live path.
    (root / "calc.py").write_text("accidental live edit\n", encoding="utf-8")
    restored = selfmod.reject(run["id"], "fixture isolation breach")

    assert restored["phase"] == "restored"
    assert (root / "calc.py").read_bytes() == original
    assert any(
        event["kind"] == "live_source_recovery"
        for event in selfmod.events(run["id"])
    )


def test_corrupted_backup_fails_closed(isolated):
    root = repository(isolated, use_git=False)
    run = plan(root)
    selfmod.create_backup(run["id"])
    manifest = json.loads(Path(selfmod.get_run(run["id"])["backup_manifest"]).read_text())
    backup = Path(manifest["files"][0]["backup_path"])
    if os.name != "nt":
        os.chmod(backup, 0o600)
    backup.write_text("corrupt", encoding="utf-8")
    with pytest.raises(RuntimeError, match="backup verification failed"):
        selfmod.verify_backup(run["id"])


def test_partial_backup_failure_removes_incomplete_bundle(monkeypatch, isolated):
    root = repository(isolated, use_git=False)
    run = plan(root, files=("calc.py", "tests/test_calc.py"))
    real = selfmod.shutil.copy2
    calls = 0
    def fail_second(source, target):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("disk failure")
        return real(source, target)
    monkeypatch.setattr(selfmod.shutil, "copy2", fail_second)
    with pytest.raises(OSError, match="disk failure"):
        selfmod.create_backup(run["id"])
    assert not (selfmod.backups_root() / run["id"]).exists()
    assert selfmod.get_run(run["id"])["phase"] == "proposed"


def test_interrupted_editing_and_explicit_resume(isolated):
    root = repository(isolated, use_git=False)
    run = prepare(plan(root))
    selfmod.claim(run["id"], "dead-owner", lease_seconds=60)
    with selfmod._tx() as conn:
        conn.execute("UPDATE selfmod_runs SET owner_pid=?,owner_host=?,lease_until=? WHERE id=?", (99999999, selfmod.socket.gethostname(), time.time() - 1, run["id"]))
    assert selfmod.reconcile_interrupted() == 1
    assert selfmod.get_run(run["id"])["phase"] == "interrupted"
    assert selfmod.resume(run["id"])["phase"] == "editing"


def test_protected_paths_and_backup_policy_require_maintenance(isolated):
    root = repository(isolated, use_git=False)
    (root / "permission_rules.py").write_text("rules=[]\n")
    (root / "selfmod.py").write_text("unsafe=True\n")
    (root / "process_liveness.py").write_text("unsafe=True\n")
    (root / "model_transport.py").write_text("unsafe=True\n")
    for path in (
        "permission_rules.py", "selfmod.py", "process_liveness.py",
        "model_transport.py",
    ):
        with pytest.raises(PermissionError, match="maintenance"):
            plan(root, files=(path,))
    approved = plan(root, files=("permission_rules.py",), risk="high", maintenance=True)
    assert approved["risk"] == "critical"
    assert approved["maintenance_authorized"]


def test_unrelated_candidate_change_is_rejected(isolated):
    root = repository(isolated)
    run = prepare(plan(root))
    (Path(run["workspace_path"]) / "unrelated.py").write_text("oops=1\n")
    with pytest.raises(RuntimeError, match="escaped approved scope"):
        selfmod.inspect_diff(run["id"])


def test_candidate_cannot_weaken_test_inventory(isolated):
    root = repository(isolated)
    run = prepare(plan(root, files=("calc.py", "tests/test_calc.py"), maintenance=True))
    selfmod.apply_candidate_changes(run["id"], {
        "calc.py": "def add(a,b): return a+b\n", "tests/test_calc.py": None,
    })
    validate(run["id"], targeted=[sys.executable, "-c", "assert True"])
    reviewed_run = selfmod.review(run["id"])
    assert reviewed_run["phase"] == "restored"
    assert "inventory was weakened" in reviewed_run["last_error"]
    assert (root / "tests/test_calc.py").is_file()


def test_candidate_cannot_rewrite_existing_test_to_pass(isolated):
    root = repository(isolated)
    run = prepare(plan(root, files=("calc.py", "tests/test_calc.py"), maintenance=True))
    selfmod.apply_candidate_changes(run["id"], {
        "calc.py": "def add(a,b): return a+b\n",
        "tests/test_calc.py": "def test_fake(): assert True\n",
    })
    selfmod.record_reproducer_before(run["id"], [sys.executable, "-c", "raise SystemExit(1)"])
    selfmod.begin_testing(run["id"])
    for kind in ("syntax", "targeted", "regression", "smoke", "security"):
        selfmod.record_test(run["id"], kind, [sys.executable, "-c", "assert True"])
    result = selfmod.review(run["id"])
    assert result["phase"] == "restored"
    assert "pre-existing required tests were modified" in result["last_error"]


def test_explicit_approval_and_auto_low_risk(isolated):
    root = repository(isolated)
    proposed = reviewed(root)
    assert proposed["phase"] == "reviewing" and proposed["approval_required"]
    assert selfmod.approve(proposed["id"], "user:test")["phase"] == "approved"

    root2 = repository(isolated / "second")
    automatic = reviewed(root2, mode="auto-low-risk")
    assert automatic["phase"] == "approved"
    assert automatic["approved_by"] == "host:auto-low-risk"


def test_high_risk_never_auto_approves(isolated):
    root = repository(isolated)
    selfmod.set_mode("auto-low-risk")
    run = selfmod.create_plan(
        "network behavior change", root, evidence=["measured timeout"], files=["calc.py"],
        criteria=["test passes"], risk="high",
    )
    prepare(run)
    selfmod.apply_candidate_changes(run["id"], {"calc.py": "def add(a,b): return a+b\n"})
    validate(run["id"])
    result = selfmod.review(run["id"])
    assert result["phase"] == "reviewing" and result["approval_required"]
    with pytest.raises(PermissionError, match="explicit user"):
        selfmod.approve(run["id"], "host:auto-low-risk")


def test_concurrent_deployment_lock_and_stale_owner(isolated):
    root = repository(isolated, use_git=False)
    run = plan(root)
    with selfmod.deployment_lock(run["id"], "owner-one"):
        with pytest.raises(RuntimeError, match="another deployment"):
            with selfmod.deployment_lock(run["id"], "owner-two"):
                pass
    with selfmod._tx() as conn:
        conn.execute("UPDATE selfmod_deployment_lock SET owner_id='dead',owner_pid=99999999,owner_host=?,lease_until=?,run_id=? WHERE id=1", (selfmod.socket.gethostname(), time.time() + 100, run["id"]))
    with selfmod.deployment_lock(run["id"], "reclaimer"):
        pass


def test_expired_lease_does_not_steal_from_live_local_owner(isolated):
    root = repository(isolated, use_git=False)
    run = plan(root)
    with selfmod._tx() as conn:
        conn.execute(
            "UPDATE selfmod_deployment_lock SET owner_id='live',owner_pid=?,owner_host=?,lease_until=?,run_id=? WHERE id=1",
            (os.getpid(), selfmod.socket.gethostname(), time.time() - 10, run["id"]),
        )
    with pytest.raises(RuntimeError, match="another deployment"):
        with selfmod.deployment_lock(run["id"], "thief"):
            pass


@pytest.mark.parametrize("use_git", [False, True])
def test_deploy_health_failure_automatically_restores(monkeypatch, isolated, use_git):
    root = repository(isolated, use_git=use_git)
    run = reviewed(root)
    selfmod.approve(run["id"], "user:test")
    original = (root / "calc.py").read_bytes()
    with pytest.raises(RuntimeError, match="automatic rollback"):
        selfmod.deploy(run["id"], health_command=[sys.executable, "-c", "raise SystemExit(9)"], commit=use_git)
    assert (root / "calc.py").read_bytes() == original
    assert selfmod.get_run(run["id"])["phase"] == "restored"


def test_interrupted_deployment_restores_partial_copy(monkeypatch, isolated):
    root = repository(isolated, use_git=False)
    run = reviewed(root, files=("calc.py", "tests/test_new.py"))
    selfmod.approve(run["id"], "user:test")
    original = hashes(root)
    real = selfmod._atomic_copy
    failed = False
    def fail_candidate(source, target, mode=None):
        nonlocal failed
        if "workspaces" in str(source) and not failed and str(target).endswith("test_new.py"):
            failed = True
            raise OSError("interrupted deploy")
        return real(source, target, mode)
    monkeypatch.setattr(selfmod, "_atomic_copy", fail_candidate)
    with pytest.raises(OSError, match="interrupted deploy"):
        selfmod.deploy(run["id"], commit=False)
    assert hashes(root) == original
    assert selfmod.get_run(run["id"])["phase"] == "restored"


def test_crashed_deployment_owner_is_reconciled_and_restored(isolated):
    root = repository(isolated, use_git=False)
    run = reviewed(root)
    selfmod.approve(run["id"], "user:test")
    original = (root / "calc.py").read_bytes()
    (root / "calc.py").write_text("partially deployed\n", encoding="utf-8")
    with selfmod._tx() as conn:
        conn.execute(
            "UPDATE selfmod_deployment_lock SET owner_id='crashed',owner_pid=99999999,owner_host=?,lease_until=?,run_id=? WHERE id=1",
            (selfmod.socket.gethostname(), time.time() - 1, run["id"]),
        )
    assert selfmod.reconcile_stale_deployment() == 1
    assert (root / "calc.py").read_bytes() == original
    assert selfmod.get_run(run["id"])["phase"] == "restored"


def test_crash_during_rollback_requested_retries_exact_restore(isolated):
    root = repository(isolated, use_git=False)
    run = reviewed(root)
    selfmod.approve(run["id"], "user:test")
    original = (root / "calc.py").read_bytes()
    (root / "calc.py").write_text("partially restored garbage\n", encoding="utf-8")
    with selfmod._tx() as conn:
        conn.execute(
            "UPDATE selfmod_runs SET phase='rollback_requested' WHERE id=?",
            (run["id"],),
        )
        conn.execute(
            "UPDATE selfmod_deployment_lock SET owner_id='crashed',owner_pid=99999999,owner_host=?,lease_until=?,run_id=? WHERE id=1",
            (selfmod.socket.gethostname(), time.time() - 1, run["id"]),
        )

    assert selfmod.reconcile_stale_deployment() == 1
    assert (root / "calc.py").read_bytes() == original
    assert selfmod.get_run(run["id"])["phase"] == "restored"


def test_git_commit_is_recovered_when_later_deploy_metadata_fails(monkeypatch, isolated):
    root = repository(isolated, use_git=True)
    starting_commit = git(root, "rev-parse", "HEAD").stdout.strip()
    run = reviewed(root)
    selfmod.approve(run["id"], "user:test")
    monkeypatch.setattr(
        selfmod,
        "_record_deployed_files",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("ledger failure")),
    )

    with pytest.raises(OSError, match="ledger failure"):
        selfmod.deploy(run["id"], commit=True)

    assert selfmod.get_run(run["id"])["phase"] == "restored"
    assert git(root, "status", "--porcelain", "--untracked-files=no").stdout == ""
    assert git(root, "rev-parse", "HEAD").stdout.strip() != starting_commit
    assert (root / "calc.py").read_text(encoding="utf-8") == "def add(a, b):\n    return a - b\n"


def test_stale_recovery_holds_global_lock_until_restore_finishes(monkeypatch, isolated):
    root = repository(isolated, use_git=False)
    run = reviewed(root)
    selfmod.approve(run["id"], "user:test")
    with selfmod._tx() as conn:
        conn.execute(
            "UPDATE selfmod_deployment_lock SET owner_id='crashed',owner_pid=99999999,owner_host=?,lease_until=?,run_id=? WHERE id=1",
            (selfmod.socket.gethostname(), time.time() - 1, run["id"]),
        )
    real_restore = selfmod.restore
    observed = {"blocked": False}

    def restore_while_probing(run_id):
        with pytest.raises(RuntimeError, match="another deployment"):
            with selfmod.deployment_lock(run_id, "competing-deployer"):
                pass
        observed["blocked"] = True
        return real_restore(run_id)

    monkeypatch.setattr(selfmod, "restore", restore_while_probing)
    assert selfmod.reconcile_stale_deployment() == 1
    assert observed["blocked"]
    assert selfmod.get_run(run["id"])["phase"] == "restored"


def test_dirty_git_worktree_preserved_and_conflicts_detected(isolated):
    root = repository(isolated)
    (root / "user.txt").write_text("user work\n")
    run = reviewed(root)
    selfmod.approve(run["id"], "user:test")
    deployed = selfmod.deploy(run["id"], health_command=[sys.executable, "-c", "from calc import add; assert add(2,3)==5"])
    assert deployed["phase"] == "deployed"
    assert (root / "user.txt").read_text() == "user work\n"
    assert deployed["deployed_commit"] in (None, "")

    root2 = repository(isolated / "conflict")
    other = reviewed(root2)
    selfmod.approve(other["id"], "user:test")
    (root2 / "user-change.txt").write_text("changed after planning\n")
    with pytest.raises(RuntimeError, match="source tree changed"):
        selfmod.deploy(other["id"], commit=False)


def test_dirty_declared_file_is_never_overwritten(isolated):
    root = repository(isolated)
    (root / "calc.py").write_text("user owned change\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="user-owned Git changes"):
        plan(root)
    assert (root / "calc.py").read_text() == "user owned change\n"


def test_retention_never_deletes_newest_valid_rollback(isolated):
    root = repository(isolated, use_git=False)
    first = plan(root)
    selfmod.create_backup(first["id"])
    time.sleep(0.02)
    second = plan(root)
    selfmod.create_backup(second["id"])
    old = selfmod._backup_dir(first["id"])
    os.utime(old, (1, 1))
    removed = selfmod.prune_backups(retention_days=1, retention_bytes=1)
    assert first["id"] in removed
    assert selfmod._backup_dir(second["id"]).exists()


def test_recursive_self_improvement_rejected(monkeypatch, isolated):
    monkeypatch.setenv("SONDER_SELFMOD_ACTIVE", "1")
    with pytest.raises(RuntimeError, match="recursive"):
        selfmod.recursive_guard()


def test_emergency_recovery_does_not_import_application(isolated):
    root = repository(isolated, use_git=False)
    run = plan(root)
    backed = selfmod.create_backup(run["id"])
    original = (root / "calc.py").read_bytes()
    (root / "calc.py").write_text("destroyed\n")
    restored = selfmod_recover.restore(backed["backup_manifest"])
    assert restored == root
    assert (root / "calc.py").read_bytes() == original


def test_end_to_end_edit_deploy_rollback_restores_every_hash(isolated):
    root = repository(isolated)
    original_hashes = hashes(root)
    run = reviewed(root, files=("calc.py", "tests/test_new.py"))
    manifest = selfmod.verify_backup(run["id"])
    assert any(not record["existed_before"] for record in manifest["files"])
    selfmod.approve(run["id"], "user:e2e")
    deployed = selfmod.deploy(
        run["id"], health_command=[sys.executable, "-c", "from calc import add; assert add(10,2)==12"]
    )
    assert deployed["phase"] == "deployed"
    assert subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=root).returncode == 0
    restored = selfmod.rollback(run["id"])
    assert restored["phase"] == "restored"
    restored_hashes = hashes(root)
    assert {path: restored_hashes[path] for path in original_hashes} == original_hashes
    assert not (root / "tests/test_new.py").exists()


def test_manual_rollback_preserves_post_deployment_user_edit(isolated):
    root = repository(isolated, use_git=False)
    run = prepare(plan(root))
    selfmod.apply_candidate_changes(run["id"], {"calc.py": "def add(a, b):\n    return a + b\n"})
    assert all(row["passed"] for row in validate(run["id"]))
    selfmod.review(run["id"])
    selfmod.approve(run["id"], "user:fixture")
    selfmod.deploy(run["id"], health_command=[sys.executable, "-c", "print('ok')"])
    user_bytes = b"# user changed this after deployment\n"
    (root / "calc.py").write_bytes(user_bytes)

    with pytest.raises(RuntimeError, match="rollback conflict"):
        selfmod.rollback(run["id"])

    assert (root / "calc.py").read_bytes() == user_bytes
    assert selfmod.get_run(run["id"])["phase"] == "deployed"


def test_manual_rollback_preserves_post_deployment_recreated_file(isolated):
    root = repository(isolated, use_git=False)
    (root / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (root / "obsolete.txt").write_text("remove me\n", encoding="utf-8")
    run = selfmod.create_plan(
        "remove obsolete fixture", root,
        problem="obsolete.txt should not exist",
        evidence=["bounded fixture confirms obsolete.txt exists"],
        files=["obsolete.txt"], criteria=["obsolete file is absent"],
        risk="low", rollback_plan="restore exact hashes",
    )
    run = prepare(run)
    selfmod.apply_candidate_changes(run["id"], {"obsolete.txt": None})
    targeted = [
        sys.executable, "-c",
        "import pathlib; assert not pathlib.Path('obsolete.txt').exists()",
    ]
    selfmod.record_reproducer_before(run["id"], targeted)
    selfmod.begin_testing(run["id"])
    for kind, command in {
        "syntax": [sys.executable, "-c", "print('no syntax target')"],
        "targeted": targeted,
        "regression": [sys.executable, "-m", "pytest", "-q"],
        "smoke": [sys.executable, "-c", "from calc import add; assert add(1, 2) == 3"],
    }.items():
        assert selfmod.record_test(run["id"], kind, command)["passed"]
    selfmod.review(run["id"])
    selfmod.approve(run["id"], "user:fixture")
    selfmod.deploy(run["id"], health_command=[sys.executable, "-c", "print('ok')"])
    replacement = b"# user recreated this path\n"
    (root / "obsolete.txt").write_bytes(replacement)

    with pytest.raises(RuntimeError, match="rollback conflict"):
        selfmod.rollback(run["id"])

    assert (root / "obsolete.txt").read_bytes() == replacement
