"""SPEC-4: end-to-end staged install, activation, rollback, and journal."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import sonder_update_engine
import sonder_updates
from sonder_update_engine import UpdateManager, confirm_nonce_for
from sonder_updates import BundleManifest, UpdateRepository, build_bundle

pytestmark = pytest.mark.integration


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Isolated state, releases dir, and the unsigned-bundle dev gate."""
    home = tmp_path / "home"
    monkeypatch.setenv("SONDER_HOME", str(home))
    monkeypatch.setenv("SONDER_DB", str(home / "memory.db"))
    monkeypatch.setenv("SONDER_AUTOPILOT_DB", str(home / "autopilot.db"))
    monkeypatch.setenv("SONDER_FLEET_DB", str(home / "fleet.db"))
    monkeypatch.setenv("SONDER_OPERATIONS_DB", str(home / "operations.db"))
    monkeypatch.setenv("SONDER_UPDATES_DB", str(home / "updates.db"))
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(home / "runtime_policy.json"))
    monkeypatch.setenv("SONDER_UPDATE_ALLOW_UNSIGNED", "1")
    return tmp_path


def _mini_source(tmp_path, name="source", *, migrate_rc=0, status_rc=0):
    source = tmp_path / name
    package = source / "sonder_runtime"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text(
        "import sys\n"
        f"rc = {migrate_rc} if 'migrate' in sys.argv else "
        f"{status_rc} if 'status' in sys.argv else 0\n"
        "print('{\"ok\": true}')\n"
        "sys.exit(rc)\n",
        encoding="utf-8",
    )
    (source / "app.py").write_text("print('app')\n", encoding="utf-8")
    return source


def _manager(env):
    return UpdateManager(
        repository=UpdateRepository(),
        releases_dir=env / "releases",
        current_link=env / "current",
        backup_target=str(env / "backups"),
    )


def _import_ok(manager, env, version="1.1.0", **source_kwargs):
    source = _mini_source(env, f"src-{version}", **source_kwargs)
    bundle = env / f"bundle-{version}"
    build_bundle(source, bundle, version=version)
    plan = manager.import_offline(bundle, allow_unverified=True)
    return plan


def test_import_verifies_and_reports_available(env):
    manager = _manager(env)
    plan = _import_ok(manager, env)
    assert plan["status"] == "available"
    assert plan["target_version"] == "1.1.0"
    assert plan["source_kind"] == "offline"


def test_import_rejects_tampered_archive(env):
    manager = _manager(env)
    source = _mini_source(env, "tampered-src")
    bundle = env / "tampered-bundle"
    result = build_bundle(source, bundle, version="1.0.1")
    archive = Path(result["archive"])
    archive.write_bytes(archive.read_bytes() + b"tamper")
    with pytest.raises(sonder_updates.TrustError, match="hash|length"):
        manager.import_offline(bundle, allow_unverified=True)


def test_import_blocks_wrong_platform(env):
    manager = _manager(env)
    source = _mini_source(env, "wrongplat-src")
    bundle = env / "wrongplat-bundle"
    build_bundle(
        source, bundle, version="1.0.2",
        platform_name="windows", architecture="x86_64",
    )
    plan = manager.import_offline(bundle, allow_unverified=True)
    assert plan["status"] == "blocked"
    assert plan["error_code"] == "INCOMPATIBLE"


def test_import_is_idempotent_with_key(env):
    manager = _manager(env)
    source = _mini_source(env, "idem-src")
    bundle = env / "idem-bundle"
    build_bundle(source, bundle, version="1.0.3")
    first = manager.import_offline(
        bundle, allow_unverified=True, idempotency_key="idem-key"
    )
    second = manager.import_offline(
        bundle, allow_unverified=True, idempotency_key="idem-key"
    )
    assert first["update_id"] == second["update_id"]


def test_install_happy_path_commits_and_activates(env):
    manager = _manager(env)
    plan = _import_ok(manager, env, version="2.0.0")
    done = manager.install(
        plan["update_id"],
        confirm=confirm_nonce_for(plan),
        allow_unverified=True,
    )
    assert done["status"] == "committed"
    assert done["backup_id"], "pre-install backup must be recorded"

    current = env / "current"
    assert current.is_symlink()
    target = Path(os.readlink(current))
    assert target.name.startswith("2.0.0-")
    assert (target / "app.py").exists()

    active = manager.repository.release_by_status("active")
    assert active["version"] == "2.0.0"

    steps = {s["step_name"]: s["status"] for s in
             manager.repository.steps(plan["update_id"])}
    assert steps["stage-and-verify"] == "ok"
    assert steps["backup"] == "ok"
    assert steps["migrate"] == "ok"
    assert steps["health-check"] == "ok"
    assert steps["activate"] == "ok"

    # The backup gate produced a verified backup on disk.
    import sonder_backup

    assert sonder_backup.list_backups(env / "backups")


def test_install_requires_confirmation_nonce(env):
    manager = _manager(env)
    plan = _import_ok(manager, env, version="2.0.1")
    with pytest.raises(sonder_updates.UpdateError, match="nonce"):
        manager.install(
            plan["update_id"], confirm="wrong", allow_unverified=True
        )
    assert manager.repository.get_plan(plan["update_id"])["status"] == "available"


def test_migration_failure_rolls_back_and_keeps_previous_active(env):
    manager = _manager(env)
    good = _import_ok(manager, env, version="3.0.0")
    manager.install(
        good["update_id"], confirm=confirm_nonce_for(good),
        allow_unverified=True,
    )
    before_target = os.readlink(env / "current")

    bad = _import_ok(manager, env, version="3.1.0", migrate_rc=1)
    result = manager.install(
        bad["update_id"], confirm=confirm_nonce_for(bad),
        allow_unverified=True,
    )
    assert result["status"] == "rolled_back"
    assert result["error_code"] == "MIGRATION_FAILED"
    # Pointer untouched; previous release still active.
    assert os.readlink(env / "current") == before_target
    active = manager.repository.release_by_status("active")
    assert active["version"] == "3.0.0"
    # Failed release evidence retained (R: failed evidence kept).
    failed = manager.repository.release_by_status("failed")
    assert failed is not None
    assert Path(failed["install_path"]).exists()


def test_health_failure_rolls_back(env):
    manager = _manager(env)
    source = _mini_source(env, "unhealthy-src")
    bundle = env / "unhealthy-bundle"
    build_bundle(
        source, bundle, version="4.0.0",
        health_checks=[{
            "kind": "command",
            "argv": ["{python}", "-c", "import sys; sys.exit(3)"],
            "timeout_seconds": 30,
        }],
    )
    plan = manager.import_offline(bundle, allow_unverified=True)
    result = manager.install(
        plan["update_id"], confirm=confirm_nonce_for(plan),
        allow_unverified=True,
    )
    assert result["status"] == "rolled_back"
    assert result["error_code"] == "HEALTH_FAILED"
    assert not (env / "current").exists() or "4.0.0" not in os.readlink(
        env / "current"
    )


def test_operator_rollback_switches_to_previous(env):
    manager = _manager(env)
    first = _import_ok(manager, env, version="5.0.0")
    manager.install(
        first["update_id"], confirm=confirm_nonce_for(first),
        allow_unverified=True,
    )
    second = _import_ok(manager, env, version="5.1.0")
    manager.install(
        second["update_id"], confirm=confirm_nonce_for(second),
        allow_unverified=True,
    )
    assert "5.1.0" in os.readlink(env / "current")
    previous = manager.repository.release_by_status("previous")
    assert previous["version"] == "5.0.0"

    with pytest.raises(sonder_updates.UpdateError, match="nonce"):
        manager.rollback(confirm="nope")

    active = manager.rollback(confirm=previous["release_id"][-8:])
    assert active["version"] == "5.0.0"
    assert "5.0.0" in os.readlink(env / "current")
    demoted = manager.repository.release_by_status("previous")
    assert demoted["version"] == "5.1.0"


def test_rollback_refused_when_previous_release_missing(env):
    import shutil

    manager = _manager(env)
    first = _import_ok(manager, env, version="6.0.0")
    manager.install(
        first["update_id"], confirm=confirm_nonce_for(first),
        allow_unverified=True,
    )
    second = _import_ok(manager, env, version="6.1.0")
    manager.install(
        second["update_id"], confirm=confirm_nonce_for(second),
        allow_unverified=True,
    )
    previous = manager.repository.release_by_status("previous")
    shutil.rmtree(previous["install_path"])
    with pytest.raises(sonder_updates.UpdateError, match="restore"):
        manager.rollback(confirm=previous["release_id"][-8:])


def test_cancel_only_before_activation(env):
    manager = _manager(env)
    plan = _import_ok(manager, env, version="7.0.0")
    cancelled = manager.cancel(plan["update_id"])
    assert cancelled["status"] == "cancelled"
    with pytest.raises(sonder_updates.UpdateError):
        manager.cancel(plan["update_id"])  # terminal now


def test_status_surfaces_releases_and_nonces(env):
    manager = _manager(env)
    plan = _import_ok(manager, env, version="8.0.0")
    status = manager.status()
    assert status["running_version"]
    mine = next(
        p for p in status["plans"] if p["update_id"] == plan["update_id"]
    )
    assert mine["confirm_nonce"] == confirm_nonce_for(plan)


def test_update_events_are_audited(env):
    manager = _manager(env)
    plan = _import_ok(manager, env, version="9.0.0")
    manager.install(
        plan["update_id"], confirm=confirm_nonce_for(plan),
        allow_unverified=True,
    )
    from sonder_operations_store import OperationsStore

    codes = {e.event_code for e in OperationsStore().recent_events(limit=50)}
    assert "UPDATE_AVAILABLE" in codes
    assert "UPDATE_COMMITTED" in codes
