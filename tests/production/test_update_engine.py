"""SPEC-4: end-to-end staged install, activation, rollback, and journal."""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import sonder_runtime.adapters.updates.service as sonder_updates
from sonder_runtime.adapters.updates.engine import UpdateManager, confirm_nonce_for
from sonder_runtime.adapters.updates.service import UpdateRepository, build_bundle

pytestmark = pytest.mark.integration


def _pointer_text(link) -> str:
    """The active release path, however this platform records it.

    Never returns None: an unresolvable pointer must fail a substring
    assertion rather than raise, so the failure names the missing release
    instead of a TypeError.
    """
    return sonder_updates._read_pointer(link) or ""


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


# The stub used to answer `migrate --json` with `{"ok": true}`, which sufficed
# only because the engine discarded the payload. Now that the migrate step
# reads what it already asks for, the stub speaks the real contract: a
# per-store report showing migrations were discovered and none remain pending.
_STUB_MIGRATE_REPORT = (
    '{"operations": {"db_path": "stub", "applied": ["0001_baseline"],'
    ' "pending": [], "unknown": [], "checksum_mismatches": [], "discovered": 1}}'
)


def _mini_source(tmp_path, name="source", *, migrate_rc=0, status_rc=0):
    source = tmp_path / name
    package = source / "sonder_runtime"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text(
        "import sys\n"
        f"rc = {migrate_rc} if 'migrate' in sys.argv else "
        f"{status_rc} if 'status' in sys.argv else 0\n"
        "if 'migrate' in sys.argv:\n"
        f"    print({_STUB_MIGRATE_REPORT!r})\n"
        "else:\n"
        "    print('{\"ok\": true}')\n"
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


@pytest.mark.parametrize(
    "name",
    (
        r"C:\\outside.tar",
        r"\\\\server\\share\\outside.tar",
        r"nested\\archive.tar",
        r"..\\outside.tar",
        "nested/archive.tar",
        "../outside.tar",
        "archive.tar:alternate-stream",
    ),
)
def test_archive_name_cannot_escape_bundle_on_any_host(env, name):
    """Windows archive spellings must not bypass the local-bundle boundary."""
    manager = _manager(env)
    bundle = env / "bundle"
    (bundle / "targets").mkdir(parents=True)

    with pytest.raises(sonder_updates.UpdateError, match="valid archive name"):
        manager._locate_archive(bundle, {"name": name})


def test_import_blocks_wrong_platform(env):
    manager = _manager(env)
    source = _mini_source(env, "wrongplat-src")
    bundle = env / "wrongplat-bundle"
    # "windows" was hardcoded as the wrong platform, which inverted the
    # test's meaning on a Windows host; pick one that is wrong for
    # whichever host actually runs the suite.
    wrong = "windows" if sonder_updates.current_platform() != "windows" else "linux"
    build_bundle(
        source, bundle, version="1.0.2",
        platform_name=wrong, architecture="x86_64",
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

    # Symlink on POSIX, `current.pointer` where a directory symlink cannot be
    # created (unprivileged Windows). Assert through the accessor the engine
    # itself reads, not through one platform's representation.
    current_target = sonder_updates._read_pointer(env / "current")
    assert current_target, "activation left no resolvable pointer"
    target = Path(current_target)
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
    from sonder_runtime.adapters import backup as sonder_backup

    assert sonder_backup.list_backups(env / "backups")


def test_backup_failure_blocks_install_without_disclosing_adapter_detail(
    env, monkeypatch
):
    from sonder_runtime.bootstrap import app as bootstrap_app

    manager = _manager(env)
    plan = _import_ok(manager, env, version="2.0.0-backup-failure")
    secret = str(env / "private" / "credential.txt")

    class FailingBackup:
        def create(self, _target):
            raise OSError(secret)

    monkeypatch.setattr(
        bootstrap_app,
        "default_app",
        lambda: SimpleNamespace(backup=FailingBackup()),
    )
    with pytest.raises(sonder_updates.UpdateError) as caught:
        manager.install(
            plan["update_id"],
            confirm=confirm_nonce_for(plan),
            allow_unverified=True,
        )

    assert secret not in str(caught.value)
    failed = manager.repository.get_plan(plan["update_id"])
    assert failed["status"] == "failed"
    assert failed["error_code"] == "BACKUP_FAILED"
    # Nothing was ever activated, so no pointer should resolve -- asserted
    # through the accessor rather than through `current` existing, which is
    # only ever the symlink representation.
    assert sonder_updates._read_pointer(env / "current") is None


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
    before_target = sonder_updates._read_pointer(env / "current")
    assert before_target, "the good install must have left a resolvable pointer"

    bad = _import_ok(manager, env, version="3.1.0", migrate_rc=1)
    result = manager.install(
        bad["update_id"], confirm=confirm_nonce_for(bad),
        allow_unverified=True,
    )
    assert result["status"] == "rolled_back"
    assert result["error_code"] == "MIGRATION_FAILED"
    # Pointer untouched; previous release still active.
    assert sonder_updates._read_pointer(env / "current") == before_target
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
    # Unconditional: `_pointer_text` reads whichever representation this
    # platform used and returns "" when there is none, so this asserts the
    # failed release is not active instead of skipping the check wherever
    # `current` happens not to be a symlink.
    assert "4.0.0" not in _pointer_text(env / "current")
    # And say what SHOULD be true, not only what should not: with no prior
    # release the rollback must leave nothing active, so "" above means
    # "rolled back" rather than "this platform records activation elsewhere".
    assert manager.repository.release_by_status("active") is None


def test_http_only_health_checks_journal_a_skip_not_a_pass(env):
    """An http-only manifest verifies nothing, so the journal must say so.

    http checks need the service listening and the offline install runs
    stopped, so they are skipped; the step used to be journaled "ok" because
    an empty problem list was read as "everything passed".
    """
    manager = _manager(env)
    source = _mini_source(env, "http-only-src")
    bundle = env / "http-only-bundle"
    build_bundle(
        source, bundle, version="4.5.0",
        health_checks=[{
            "kind": "http",
            "url": "http://127.0.0.1:11435/v1/sonder/status",
            "expect_status": 200,
        }],
    )
    plan = manager.import_offline(bundle, allow_unverified=True)
    done = manager.install(
        plan["update_id"], confirm=confirm_nonce_for(plan),
        allow_unverified=True,
    )
    assert done["status"] == "committed"
    step = next(
        s for s in manager.repository.steps(plan["update_id"])
        if s["step_name"] == "health-check"
    )
    assert step["status"] == "skipped"
    assert step["evidence"]["skipped"] == [
        "http://127.0.0.1:11435/v1/sonder/status"
    ]


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
    assert "5.1.0" in _pointer_text(env / "current")
    previous = manager.repository.release_by_status("previous")
    assert previous["version"] == "5.0.0"

    with pytest.raises(sonder_updates.UpdateError, match="nonce"):
        manager.rollback(confirm="nope")

    active = manager.rollback(confirm=previous["release_id"][-8:])
    assert active["version"] == "5.0.0"
    assert "5.0.0" in _pointer_text(env / "current")
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
    from sonder_runtime.adapters.persistence.operations_store import OperationsStore

    codes = {e.event_code for e in OperationsStore().recent_events(limit=50)}
    assert "UPDATE_AVAILABLE" in codes
    assert "UPDATE_COMMITTED" in codes


# ---------------------------------------------------------------------------
# The migrate step asked for --json, threw the payload away, and trusted the
# exit code. Combined with `migrate_store`'s early return on an empty
# discovery, a release shipped WITHOUT its `migrations/` directory journalled
# "ok" and activated. These pin the step to the payload it already requests.
# ---------------------------------------------------------------------------


def _payload_source(tmp_path, name, payload_literal):
    """A release whose `migrate --json` prints exactly *payload_literal*."""
    source = tmp_path / name
    package = source / "sonder_runtime"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text(
        "import sys\n"
        "if 'migrate' in sys.argv:\n"
        f"    print({payload_literal!r})\n"
        "    sys.exit(0)\n"
        "print('{\"ok\": true}')\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    (source / "app.py").write_text("print('app')\n", encoding="utf-8")
    return source


def _install_with_payload(manager, env, version, payload_literal):
    source = _payload_source(env, f"payload-{version}", payload_literal)
    bundle = env / f"payload-bundle-{version}"
    build_bundle(source, bundle, version=version)
    plan = manager.import_offline(bundle, allow_unverified=True)
    return manager.install(
        plan["update_id"], confirm=confirm_nonce_for(plan), allow_unverified=True
    )


HEALTHY_PAYLOAD = (
    '{"operations": {"db_path": "x", "applied": ["0001_baseline"], "pending": [],'
    ' "unknown": [], "checksum_mismatches": [], "discovered": 1}}'
)


def test_migrate_accepts_a_payload_that_shows_migrations_were_discovered(env):
    manager = _manager(env)
    done = _install_with_payload(manager, env, "4.0.0", HEALTHY_PAYLOAD)
    assert done["status"] == "committed"
    steps = {s["step_name"]: s["status"] for s in
             manager.repository.steps(done["update_id"])}
    assert steps["migrate"] == "ok"


def test_migrate_refuses_a_release_that_shipped_no_migrations(env):
    """Every store discovering zero migrations means `migrations/` is missing."""
    manager = _manager(env)
    payload = (
        '{"operations": {"db_path": "x", "applied": [], "pending": [],'
        ' "unknown": [], "checksum_mismatches": [], "discovered": 0}}'
    )
    result = _install_with_payload(manager, env, "4.1.0", payload)
    assert result["status"] == "rolled_back"
    assert result["error_code"] == "MIGRATION_FAILED"


def test_migrate_refuses_when_migrations_remain_pending(env):
    manager = _manager(env)
    payload = (
        '{"operations": {"db_path": "x", "applied": [], "pending": ["0002_next"],'
        ' "unknown": [], "checksum_mismatches": [], "discovered": 2}}'
    )
    result = _install_with_payload(manager, env, "4.2.0", payload)
    assert result["status"] == "rolled_back"
    assert result["error_code"] == "MIGRATION_FAILED"


def test_migrate_refuses_an_unreadable_payload(env):
    """Exit 0 with output nothing can parse is not evidence of a migration."""
    manager = _manager(env)
    result = _install_with_payload(manager, env, "4.3.0", "migrations done!")
    assert result["status"] == "rolled_back"
    assert result["error_code"] == "MIGRATION_FAILED"


def test_migrate_refuses_a_payload_reporting_an_unknown_ledger(env):
    manager = _manager(env)
    payload = (
        '{"operations": {"db_path": "x", "applied": [], "pending": [],'
        ' "unknown": ["9999_future"], "checksum_mismatches": [], "discovered": 1}}'
    )
    result = _install_with_payload(manager, env, "4.4.0", payload)
    assert result["status"] == "rolled_back"
    assert result["error_code"] == "MIGRATION_FAILED"


def test_migrate_refuses_a_payload_with_no_stores_at_all(env):
    manager = _manager(env)
    result = _install_with_payload(manager, env, "4.5.0", "{}")
    assert result["status"] == "rolled_back"
    assert result["error_code"] == "MIGRATION_FAILED"
