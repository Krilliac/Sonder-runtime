"""Staged installation, atomic activation, and rollback (SPEC-4 WP4/WP5).

Drives an update plan through the SPEC-4 state machine with the SPEC-2
gates in order: trust revalidation, compatibility preflight, verified
backup, drain, staged extraction (never in place), migration, health
checks, atomic pointer switch, commit — and rollback on any activation
failure.  Every step lands in the durable update journal.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import sonder_migrations
import sonder_paths
import sonder_updates
import sonder_version
from sonder_updates import (
    BundleManifest,
    CANCELLABLE_STATES,
    CompatibilityError,
    TrustError,
    UpdateError,
    UpdateRepository,
    check_compatibility,
    current_architecture,
    current_platform,
    safe_extract,
    verify_bundle_trust,
    _sha256_file,
)


def default_releases_dir() -> Path:
    override = os.environ.get("SONDER_RELEASES_DIR", "").strip()
    return Path(override).expanduser() if override else sonder_paths.default_machine_home() / "releases"


def default_current_link() -> Path:
    override = os.environ.get("SONDER_CURRENT_LINK", "").strip()
    return Path(override).expanduser() if override else sonder_paths.default_machine_home() / "current"


def confirm_nonce_for(plan: dict) -> str:
    """The explicit confirmation nonce for install/rollback (R-M6)."""
    return plan["update_id"][-8:]


class UpdateManager:
    def __init__(
        self,
        *,
        repository: UpdateRepository | None = None,
        releases_dir: Path | None = None,
        current_link: Path | None = None,
        backup_target: str | None = None,
        operations=None,
        drain_hook=None,
        restart_hook=None,
        health_timeout: float = 120.0,
    ) -> None:
        self.repository = repository or UpdateRepository()
        self.releases_dir = Path(releases_dir or default_releases_dir())
        self.current_link = Path(current_link or default_current_link())
        self._backup_target = backup_target
        self._operations = operations
        self._drain_hook = drain_hook
        self._restart_hook = restart_hook
        self._health_timeout = health_timeout

    # -- helpers -----------------------------------------------------------

    def _ops(self):
        if self._operations is None:
            try:
                from sonder_operations_store import OperationsStore

                self._operations = OperationsStore()
            except Exception:
                return None
        return self._operations

    def _event(self, code: str, summary: str, detail: dict, *, severity="INFO",
               operation_id: str | None = None) -> None:
        ops = self._ops()
        if ops is None:
            return
        try:
            ops.record_event(
                component="updates", event_code=code, severity=severity,
                summary=summary, detail=detail, operation_id=operation_id,
            )
        except Exception:
            pass

    def _current_release_id(self) -> str:
        active = self.repository.release_by_status("active")
        if active:
            return active["release_id"]
        return "rel_source_checkout"

    # -- import / check ----------------------------------------------------

    def import_offline(
        self,
        bundle_dir: str | os.PathLike,
        *,
        channel: str = "stable",
        allow_unverified: bool = False,
        idempotency_key: str | None = None,
    ) -> dict:
        """Verify an offline bundle and persist an AVAILABLE/BLOCKED plan."""
        bundle = Path(bundle_dir).expanduser().resolve()
        manifest = BundleManifest.load(bundle / "manifest.json")
        trust_mode = verify_bundle_trust(
            bundle, manifest, allow_unverified=allow_unverified
        )
        archive_info = manifest.get("archive") or {}
        archive = self._locate_archive(bundle, archive_info)
        actual = _sha256_file(archive)
        if actual != archive_info.get("sha256"):
            raise TrustError(
                f"archive hash mismatch for {archive.name}: refusing bundle"
            )
        if archive.stat().st_size != archive_info.get("length"):
            raise TrustError(f"archive length mismatch for {archive.name}")

        self.releases_dir.mkdir(parents=True, exist_ok=True)
        problems = check_compatibility(
            manifest, releases_dir=self.releases_dir
        )
        plan = self.repository.create_plan(
            channel=channel,
            source_kind="offline",
            source_ref=str(bundle),
            from_release_id=self._current_release_id(),
            target_version=manifest["version"],
            target_manifest_sha256=manifest.sha256(),
            status="planned",
            idempotency_key=idempotency_key,
        )
        if plan["status"] != "planned":
            return plan  # idempotent replay of an existing plan
        plan = self.repository.advance(plan, "checking")
        if problems:
            plan = self.repository.advance(
                plan, "blocked",
                error_code="INCOMPATIBLE",
                error_detail="; ".join(problems)[:512],
            )
            self._event(
                "UPDATE_BLOCKED", "bundle incompatible",
                {"version": manifest["version"], "problems": len(problems)},
                severity="WARNING", operation_id=plan["update_id"],
            )
        else:
            plan = self.repository.advance(plan, "available")
            self._event(
                "UPDATE_AVAILABLE", "bundle imported and verified",
                {"version": manifest["version"], "trust": trust_mode},
                operation_id=plan["update_id"],
            )
        return plan

    def _locate_archive(self, bundle: Path, archive_info: dict) -> Path:
        name = archive_info.get("name", "")
        if not name or "/" in name or name.startswith("."):
            raise UpdateError("manifest lacks a valid archive name")
        for candidate in (bundle / "targets" / name, bundle / name):
            if candidate.is_file():
                return candidate
        raise UpdateError(f"bundle archive {name!r} not found in {bundle}")

    def _manifest_for(self, plan: dict) -> tuple[BundleManifest, Path]:
        bundle = Path(plan["source_ref"])
        manifest = BundleManifest.load(bundle / "manifest.json")
        if manifest.sha256() != plan["target_manifest_sha256"]:
            raise TrustError(
                "bundle manifest changed since the plan was verified"
            )
        return manifest, bundle

    # -- install -----------------------------------------------------------

    def install(
        self,
        update_id: str,
        *,
        confirm: str,
        allow_unverified: bool = False,
        skip_backup: bool = False,
    ) -> dict:
        plan = self.repository.get_plan(update_id)
        if plan["status"] != "available":
            raise UpdateError(
                f"plan {update_id} is {plan['status']}, not available"
            )
        if confirm != confirm_nonce_for(plan):
            raise UpdateError(
                "confirmation nonce mismatch; run `update status` for the "
                "required nonce"
            )
        manifest, bundle = self._manifest_for(plan)
        ops = self._ops()
        owner = f"update-{os.getpid()}"
        if ops is not None:
            ops.acquire_maintenance_lock(
                "update", owner_id=owner,
                reason=f"installing {manifest['version']}",
                ttl_seconds=2 * 3600,
            )
        step = 0
        staging: Path | None = None
        try:
            # downloading/verified/staged: offline bundles are already local;
            # the states still advance so the journal is uniform.
            plan = self.repository.advance(plan, "downloading")
            archive = self._locate_archive(bundle, manifest.get("archive") or {})
            step += 1
            self.repository.record_step(
                update_id, step, "revalidate-trust", "ok",
                evidence={"archive": archive.name},
            )
            verify_bundle_trust(
                bundle, manifest, allow_unverified=allow_unverified
            )
            if _sha256_file(archive) != (manifest.get("archive") or {}).get(
                "sha256"
            ):
                raise TrustError("archive hash changed since import")
            plan = self.repository.advance(plan, "verified")

            # Stage: extract into a fresh directory, never in place (R-M8).
            version = manifest["version"]
            sha8 = manifest["commit_sha"][:8] if manifest["commit_sha"] != "unknown" else plan["update_id"][-8:]
            final_dir = self.releases_dir / f"{version}-{sha8}"
            if final_dir.exists():
                raise UpdateError(
                    f"release directory {final_dir} already exists"
                )
            staging = self.releases_dir / f".staging-{plan['update_id']}"
            shutil.rmtree(staging, ignore_errors=True)
            budget = max(
                int(manifest["resources"].get("installed_bytes", 0)) * 2,
                64 * 1024 * 1024,
            )
            step += 1
            written = safe_extract(archive, staging, max_expanded_bytes=budget)
            problems = manifest.verify_tree(staging)
            if problems:
                raise UpdateError(
                    "staged tree failed manifest verification: "
                    + "; ".join(problems[:5])
                )
            self.repository.record_step(
                update_id, step, "stage-and-verify", "ok",
                evidence={"bytes": written, "files": len(manifest["files"])},
            )
            plan = self.repository.advance(plan, "staged")

            # Preflight: compatibility + disk, again, this close to commit.
            plan = self.repository.advance(plan, "preflight")
            problems = check_compatibility(
                manifest, releases_dir=self.releases_dir
            )
            step += 1
            if problems:
                self.repository.record_step(
                    update_id, step, "preflight", "failed",
                    evidence={"problems": problems[:5]},
                    error_code="INCOMPATIBLE",
                )
                plan = self.repository.advance(
                    plan, "blocked", error_code="INCOMPATIBLE",
                    error_detail="; ".join(problems)[:512],
                )
                raise CompatibilityError("; ".join(problems))
            self.repository.record_step(update_id, step, "preflight", "ok")

            # Verified backup (R-M9) before anything irreversible.
            plan = self.repository.advance(plan, "backing_up")
            step += 1
            backup_id = None
            if skip_backup:
                self.repository.record_step(
                    update_id, step, "backup", "skipped",
                    evidence={"reason": "explicitly skipped by operator"},
                )
            else:
                from sonder_runtime.bootstrap.app import default_app

                target = self._backup_target or str(
                    Path(sonder_migrations.store_db_paths()["operations"])
                    .parent / "backups"
                )
                try:
                    result = default_app().backup.create(target)
                    backup_id = result.backup_id
                    self.repository.record_step(
                        update_id, step, "backup", "ok",
                        evidence={"backup_id": backup_id},
                    )
                except Exception as exc:
                    self.repository.record_step(
                        update_id, step, "backup", "failed",
                        error_code=type(exc).__name__,
                    )
                    plan = self.repository.advance(
                        plan, "failed", error_code="BACKUP_FAILED",
                        error_detail=type(exc).__name__,
                    )
                    raise UpdateError(f"backup failed: {exc}") from exc
            plan = self.repository.advance(plan, "draining", backup_id=backup_id)

            # Drain the runtime (hook provided by the caller when a live
            # service is running; command-line installs run stopped).
            step += 1
            if self._drain_hook is not None:
                self._drain_hook()
                self.repository.record_step(update_id, step, "drain", "ok")
            else:
                self.repository.record_step(
                    update_id, step, "drain", "skipped",
                    evidence={"reason": "no running service attached"},
                )
            plan = self.repository.advance(plan, "installing")

            # Atomic publish of the staged tree into the versioned path.
            step += 1
            os.rename(staging, final_dir)
            staging = None
            self.repository.record_step(
                update_id, step, "install", "ok",
                evidence={"path": str(final_dir)},
            )
            plan = self.repository.advance(plan, "migrating")

            # Run migrations using the target release's code (R-M9).
            step += 1
            migrate_result = self._run_in_release(
                final_dir, ["-m", "sonder_runtime", "migrate", "--json"],
                timeout=self._health_timeout,
            )
            if migrate_result.returncode != 0:
                self.repository.record_step(
                    update_id, step, "migrate", "failed",
                    error_code="MIGRATION_FAILED",
                    evidence={"rc": migrate_result.returncode},
                )
                return self._roll_back_activation(
                    plan, final_dir, step,
                    error_code="MIGRATION_FAILED",
                )
            self.repository.record_step(update_id, step, "migrate", "ok")
            plan = self.repository.advance(plan, "health_check")

            # Health checks from the manifest, run against the new release.
            step += 1
            skipped_checks: list[str] = []
            health_problems = self._run_health_checks(
                manifest, final_dir, skipped=skipped_checks
            )
            if health_problems:
                self.repository.record_step(
                    update_id, step, "health-check", "failed",
                    evidence={"problems": health_problems[:5]},
                    error_code="HEALTH_FAILED",
                )
                return self._roll_back_activation(
                    plan, final_dir, step, error_code="HEALTH_FAILED",
                )
            executed = len(manifest["health_checks"]) - len(skipped_checks)
            if executed > 0:
                self.repository.record_step(
                    update_id, step, "health-check", "ok",
                    evidence={"executed": executed, "skipped": skipped_checks},
                )
            else:
                # No check actually ran, so an empty problem list is not a
                # pass: a manifest carrying only http checks (skipped because
                # the offline install runs stopped) used to journal
                # health-check "ok" and activate a release nothing verified.
                self.repository.record_step(
                    update_id, step, "health-check", "skipped",
                    evidence={
                        "reason": "manifest declares no runnable health check",
                        "skipped": skipped_checks,
                    },
                )

            # Atomic pointer switch (R-M10).
            step += 1
            previous_target = self._switch_pointer(final_dir)
            self.repository.record_step(
                update_id, step, "activate", "ok",
                evidence={
                    "current": str(final_dir),
                    "previous": previous_target or "",
                },
            )

            # Book-keeping: releases table, then COMMITTED.
            active = self.repository.release_by_status("active")
            if active:
                self.repository.set_release_status(
                    active["release_id"], "previous"
                )
            release_id = f"rel_{plan['update_id'][4:]}"
            self.repository.record_release(
                release_id=release_id,
                version=manifest["version"],
                commit_sha=manifest["commit_sha"],
                platform_name=manifest["platform"],
                architecture=manifest["architecture"],
                install_path=str(final_dir),
                status="active",
                manifest_sha256=plan["target_manifest_sha256"],
                state_schema=manifest["state_schema"],
            )
            plan = self.repository.advance(plan, "committed")
            self._event(
                "UPDATE_COMMITTED", f"release {manifest['version']} active",
                {"version": manifest["version"], "release_id": release_id},
                operation_id=update_id,
            )
            if self._restart_hook is not None:
                self._restart_hook()
            return plan
        finally:
            if staging is not None:
                shutil.rmtree(staging, ignore_errors=True)
            if ops is not None:
                ops.release_maintenance_lock("update", owner_id=owner)

    def _roll_back_activation(
        self, plan: dict, failed_dir: Path, step: int, *, error_code: str
    ) -> dict:
        """Migration/health failure after staging: keep the pointer where it
        is, retain the failed release for evidence (R-M11), and record."""
        plan = self.repository.advance(
            plan, "rolling_back", error_code=error_code
        )
        # The pointer was never switched, so the previous release is still
        # active; nothing to restore beyond marking the failed target.
        release_id = f"rel_{plan['update_id'][4:]}"
        self.repository.record_release(
            release_id=release_id,
            version=plan["target_version"],
            commit_sha="unknown",
            platform_name=current_platform(),
            architecture=current_architecture(),
            install_path=str(failed_dir),
            status="failed",
            manifest_sha256=plan["target_manifest_sha256"],
            state_schema={},
        )
        plan = self.repository.advance(plan, "rolled_back")
        self._event(
            "UPDATE_ROLLED_BACK",
            f"activation failed ({error_code}); previous release retained",
            {"error_code": error_code, "failed_release": str(failed_dir)},
            severity="WARNING", operation_id=plan["update_id"],
        )
        return plan

    def _switch_pointer(self, target: Path) -> str | None:
        """Atomically point ``current`` at ``target``; return the old target.

        Delegates to the portable activation helper so unprivileged Windows
        (no directory-symlink right) falls back to an atomic pointer file
        (R-M10, R-M18 handoff safety).
        """
        return sonder_updates.switch_active_pointer(self.current_link, target)

    def _run_in_release(
        self, release_dir: Path, args: list[str], *, timeout: float
    ) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(release_dir)
        return subprocess.run(
            [sys.executable, *args],
            cwd=release_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def _run_health_checks(
        self,
        manifest: BundleManifest,
        release_dir: Path,
        *,
        skipped: list[str] | None = None,
    ) -> list[str]:
        """Run the manifest's health checks; append skipped ones to ``skipped``.

        An empty problem list alone cannot tell the caller whether every check
        passed or no check ran, so the skips are reported out of band.
        """
        problems: list[str] = []
        for check in manifest["health_checks"]:
            kind = check.get("kind")
            if kind == "command":
                raw_argv = check.get("argv", [])
                if not raw_argv:
                    problems.append("health check with empty argv")
                    continue
                # V1 defense-in-depth: even though manifest.json is now a
                # signed TUF target, constrain argv[0] to the release's own
                # interpreter placeholder ("{python}") so a compromised or
                # mistaken manifest can never execute an arbitrary external
                # program during health_check. The check always runs against
                # the target release's code via _run_in_release.
                if raw_argv[0] != "{python}":
                    problems.append(
                        f"health check argv[0] {raw_argv[0]!r} is not the "
                        "release interpreter ('{python}'); refusing to "
                        "execute an external program"
                    )
                    continue
                argv = [
                    sys.executable if part == "{python}" else part
                    for part in raw_argv
                ]
                timeout = float(
                    check.get("timeout_seconds", self._health_timeout)
                )
                try:
                    result = self._run_in_release(
                        release_dir, argv[1:], timeout=timeout
                    )
                except (OSError, subprocess.SubprocessError) as exc:
                    problems.append(f"{argv[0]}: {type(exc).__name__}")
                    continue
                if result.returncode != 0:
                    problems.append(
                        f"{' '.join(argv[:3])}... exited {result.returncode}"
                    )
            elif kind == "http":
                # HTTP checks need the service running on the verification
                # port; the offline CLI install runs stopped, so record the
                # skip rather than fake a pass.
                if skipped is not None:
                    skipped.append(str(check.get("url") or "http"))
                continue
            else:
                problems.append(f"unknown health check kind {kind!r}")
        return problems

    # -- rollback (operator-initiated, R-M11) ------------------------------

    def rollback(self, *, confirm: str) -> dict:
        active = self.repository.release_by_status("active")
        previous = self.repository.release_by_status("previous")
        if previous is None:
            raise UpdateError("no previous release is recorded to roll back to")
        if confirm != previous["release_id"][-8:]:
            raise UpdateError(
                "confirmation nonce mismatch; pass the last 8 characters of "
                f"the previous release id ({previous['release_id']})"
            )
        target = Path(previous["install_path"])
        if not target.is_dir():
            raise UpdateError(
                f"previous release directory {target} is missing; "
                "state restore from backup is required"
            )
        self._switch_pointer(target)
        # Demote first: the partial unique index allows exactly one 'active'.
        if active:
            self.repository.set_release_status(active["release_id"], "previous")
        self.repository.set_release_status(previous["release_id"], "active")
        self._event(
            "UPDATE_ROLLED_BACK",
            f"operator rollback to {previous['version']}",
            {"release_id": previous["release_id"],
             "version": previous["version"]},
            severity="WARNING",
        )
        if self._restart_hook is not None:
            self._restart_hook()
        return self.repository.release_by_status("active")

    # -- cancel / status ---------------------------------------------------

    def cancel(self, update_id: str) -> dict:
        plan = self.repository.get_plan(update_id)
        if plan["status"] not in CANCELLABLE_STATES:
            raise UpdateError(
                f"plan in state {plan['status']} can no longer be cancelled"
            )
        return self.repository.advance(plan, "cancelled")

    def status(self) -> dict:
        build = sonder_version.build_info()
        # Symlink or pointer-file fallback, whichever this platform used.
        current_target = sonder_updates._read_pointer(self.current_link)
        plans = self.repository.list_plans(limit=10)
        return {
            "running_version": build.version,
            "running_commit": build.commit_sha,
            "platform": current_platform(),
            "architecture": current_architecture(),
            "current_link": str(self.current_link),
            "current_target": current_target,
            "active_release": self.repository.release_by_status("active"),
            "previous_release": self.repository.release_by_status("previous"),
            "plans": [
                {
                    **{k: p[k] for k in (
                        "update_id", "status", "channel", "target_version",
                        "created_at_utc", "error_code",
                    )},
                    "confirm_nonce": confirm_nonce_for(p)
                    if p["status"] == "available" else None,
                }
                for p in plans
            ],
        }
