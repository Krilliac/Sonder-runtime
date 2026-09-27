"""``python -m sonder_runtime`` — production command surface (SPEC-2 WP1).

Commands:

    serve         run the HTTP adapter (preflight first, fail closed)
    mcp           run the MCP adapter
    repl          run the interactive REPL
    preflight     run startup checks and report without binding
    doctor        consolidated read-only runtime health report
    status        local runtime/build/schema status
    diagnostics   privacy-safe diagnostic bundle (redacted)
    config        show the effective redacted configuration
    migrate       apply pending schema migrations
    backup        create / verify / list / prune backups
    restore       verify / apply a backup into an empty directory, or
                  rehearse restore + failed-upgrade rollback in a disposable tree
    drain         request graceful drain of a running server
    smoke         minimal end-to-end check without a real model
    control-state-rehearsal  collect disposable provider evidence without promotion
    eval-history  inspect or explicitly record precomputed evaluation evidence
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import sys

from sonder_runtime.adapters.persistence.migrations import STORE_NAMES
from sonder_runtime.domain.artifact_mobility_label import is_public_mobility_label
from sonder_runtime.platform import config as sonder_config
from sonder_runtime.platform import paths as runtime_paths
from sonder_runtime.platform import version as sonder_version
from sonder_runtime.application.command_surface import McpCommand
from sonder_runtime.bootstrap.legacy_mcp import build_legacy_server_mcp_runtime


def _cli_overrides(args) -> dict:
    overrides = {}
    for item in getattr(args, "set", None) or []:
        if "=" not in item:
            raise sonder_config.ConfigError(
                [f"--set expects section.key=value, got {item!r}"]
            )
        key, _, value = item.partition("=")
        overrides[key.strip()] = value.strip()
    return overrides


def _load_config(args) -> "sonder_config.SonderConfig":
    overrides = _cli_overrides(args)
    # Preserve the legacy ``python sonder_serve.py [port]`` launcher contract
    # for the packaged ``python -m sonder_runtime serve [port]`` entrypoint.
    if getattr(args, "port", None) is not None:
        overrides["server.port"] = str(args.port)
    return sonder_config.load_config(
        _configured_path(getattr(args, "config", None), "SONDER_CONFIG", "sonder.toml"),
        secrets_path=_configured_path(
            getattr(args, "secrets", None), "SONDER_SECRETS", "sonder.env"
        ),
        overrides=overrides or None,
    )


def _configured_path(explicit, env_name: str, filename: str):
    """Resolve the one user-global config location without requiring flags.

    An explicit CLI/environment path is authoritative and therefore reported if
    it is missing.  The conventional per-user file is optional: first-run
    installations continue to use the typed safe defaults until it exists.
    """
    if explicit:
        return explicit
    configured = os.environ.get(env_name, "").strip()
    if configured:
        return configured
    candidate = sonder_config.sonder_paths.default_home() / filename
    return str(candidate) if candidate.is_file() else None


def _redactor_for_config(config):
    """Create the common redactor before compatibility env export occurs."""
    from sonder_runtime.platform.logging import redactor_for_config

    return redactor_for_config(config, env=os.environ)


def _emit(payload: dict, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
        return
    def walk(value, indent=0):
        pad = "  " * indent
        if isinstance(value, dict):
            for key, item in value.items():
                if isinstance(item, (dict, list)):
                    print(f"{pad}{key}:")
                    walk(item, indent + 1)
                else:
                    print(f"{pad}{key}: {item}")
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, (dict, list)):
                    walk(item, indent)
                    print()
                else:
                    print(f"{pad}- {item}")
    walk(payload)


_REHEARSAL_PREFIX = "rehearsal-"
_REHEARSAL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_REHEARSAL_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_MAX_REHEARSAL_SEQUENCE = (1 << 63) - 1


def _contains_rehearsal_literal(argv: list[str]) -> bool:
    """Identify raw invocations that could expose rehearsal-only input."""
    return any(value == "control-state-rehearsal" for value in argv)


def _rehearsal_report(status: str, *, reason: str | None = None) -> dict[str, object]:
    """Return the stable, content-free report shared by every outcome.

    The explicit command can collect provider transport evidence, but it never
    owns a runtime transition.  Keep these properties present for success and
    every refusal so callers cannot mistake a capability declaration or fence
    receipt for automatic availability.
    """
    report: dict[str, object] = {
        "schema": "sonder.control-state-rehearsal.v1",
        "status": status,
        "evidence_scope": "process-boundary-transport-rehearsal",
        "promotion_attempted": False,
        "automatic_takeover_available": False,
        "automatic_failback_available": False,
    }
    if reason is not None:
        report["reason"] = reason
    return report


def _emit_rehearsal_report(args, report: dict[str, object]) -> None:
    """Emit a report without config paths, transport bodies, or exceptions."""
    _emit(report, as_json=bool(getattr(args, "json", False)))


def _rehearsal_identity(value: object) -> bool:
    return isinstance(value, str) and _REHEARSAL_ID.fullmatch(value) is not None


def _rehearsal_request_error(args) -> str | None:
    """Reject all caller-controlled scope before config/factory/provider work."""
    config_path = getattr(args, "config", None)
    if not isinstance(config_path, str) or not config_path.strip():
        return "configuration_invalid"
    secrets_path = getattr(args, "secrets", None)
    if secrets_path is not None and (
        not isinstance(secrets_path, str) or not secrets_path.strip()
    ):
        return "configuration_invalid"

    confirmation = getattr(args, "confirm_fence", None)
    new_owner_id = getattr(args, "new_owner_id", None)
    if confirmation is not None and confirmation != "external-fence":
        return "invalid_confirmation"
    if confirmation == "external-fence" and not new_owner_id:
        return "new_owner_required"
    if confirmation is None and new_owner_id is not None:
        return "new_owner_without_confirmation"

    event_id = getattr(args, "event_id", None)
    if (
        not _rehearsal_identity(event_id)
        or not event_id.startswith(_REHEARSAL_PREFIX)
        or len(event_id) == len(_REHEARSAL_PREFIX)
    ):
        return "rehearsal_event_required"
    resource_id = getattr(args, "resource_id", None)
    if (
        not _rehearsal_identity(resource_id)
        or not resource_id.startswith(_REHEARSAL_PREFIX)
        or len(resource_id) == len(_REHEARSAL_PREFIX)
    ):
        return "rehearsal_resource_required"
    if getattr(args, "resource_kind", None) != "job":
        return "rehearsal_job_required"
    if not _rehearsal_identity(new_owner_id) and new_owner_id is not None:
        return "new_owner_invalid"
    for field, reason in (("owner_epoch", "owner_epoch_invalid"), ("sequence", "sequence_invalid")):
        value = getattr(args, field, None)
        if type(value) is not int or not 1 <= value <= _MAX_REHEARSAL_SEQUENCE:
            return reason
    if not isinstance(getattr(args, "payload_digest", None), str) or (
        _REHEARSAL_DIGEST.fullmatch(args.payload_digest) is None
    ):
        return "payload_digest_invalid"
    return None


def _rehearsal_config_boundary(config) -> tuple[str, str, str] | None:
    """Return configured rehearsal identities without constructing a provider.

    The factory remains the canonical topology validator.  This narrow preflight
    only prevents a command-line request from reaching it with a non-disposable
    cluster or an uninspectable peer identity.
    """
    section = getattr(config, "control_state_rehearsal", None)
    cluster_id = getattr(section, "cluster_id", None)
    local_id = getattr(section, "node_id", None)
    compute = getattr(config, "compute", None)
    nodes = getattr(compute, "nodes", None)
    if (
        not _rehearsal_identity(cluster_id)
        or not cluster_id.startswith(_REHEARSAL_PREFIX)
        or len(cluster_id) == len(_REHEARSAL_PREFIX)
        or not _rehearsal_identity(local_id)
        or type(nodes) is not tuple
        or len(nodes) != 1
    ):
        return None
    peer_id = getattr(nodes[0], "node_id", None)
    if not _rehearsal_identity(peer_id) or peer_id == local_id:
        return None
    return cluster_id, local_id, peer_id


def _rehearsal_event_report(event, acknowledgement) -> dict[str, object]:
    """Select only bounded, non-payload evidence from exact receipts."""
    return {
        "cluster_id": event.cluster_id,
        "event": {
            "event_id": event.event_id,
            "resource_kind": event.resource_kind,
            "resource_id": event.resource_id,
            "owner_id": event.owner_id,
            "owner_epoch": event.owner_epoch,
            "sequence": event.sequence,
        },
        "acknowledgement": {
            "provider_id": acknowledgement.provider_id,
            "durable": acknowledgement.durable,
            "data_replica_ids": list(acknowledgement.data_replica_ids),
            "witness_ids": list(acknowledgement.witness_ids),
            "data_replica_count": acknowledgement.data_replica_count,
        },
    }


def cmd_control_state_rehearsal(args) -> int:
    """Collect one disposable provider evidence page; never promote an owner."""
    request_error = _rehearsal_request_error(args)
    if request_error is not None:
        _emit_rehearsal_report(args, _rehearsal_report("rejected", reason=request_error))
        return 2

    try:
        config = _load_config(args)
    except (OSError, UnicodeError, sonder_config.ConfigError):
        _emit_rehearsal_report(
            args, _rehearsal_report("rejected", reason="configuration_invalid")
        )
        return 2

    boundary = _rehearsal_config_boundary(config)
    if boundary is None:
        _emit_rehearsal_report(
            args, _rehearsal_report("rejected", reason="rehearsal_cluster_required")
        )
        return 2
    cluster_id, local_id, peer_id = boundary
    if args.confirm_fence == "external-fence" and args.new_owner_id != peer_id:
        _emit_rehearsal_report(
            args,
            _rehearsal_report("rejected", reason="new_owner_not_configured_peer"),
        )
        return 2

    # These imports are intentionally local.  Ordinary serve, MCP, and REPL
    # command composition must not obtain a rehearsal provider by importing the
    # production entrypoint.
    from .bootstrap.control_state_rehearsal import build_control_state_rehearsal
    from .domain.cluster_availability import ControlStateEvent
    from .domain.common.errors import DependencyUnavailable

    try:
        coordinator = build_control_state_rehearsal(config)
    except (TypeError, ValueError):
        _emit_rehearsal_report(
            args, _rehearsal_report("rejected", reason="configuration_invalid")
        )
        return 2

    try:
        event = ControlStateEvent(
            event_id=args.event_id,
            cluster_id=cluster_id,
            resource_kind=args.resource_kind,
            resource_id=args.resource_id,
            owner_id=local_id,
            owner_epoch=args.owner_epoch,
            sequence=args.sequence,
            payload_digest=args.payload_digest,
        )
    except (TypeError, ValueError):
        _emit_rehearsal_report(
            args, _rehearsal_report("rejected", reason="invalid_request")
        )
        return 2

    try:
        acknowledgement = coordinator.append(event)
        page = coordinator.read(
            event.cluster_id,
            after_sequence=event.sequence - 1,
            limit=1,
        )
        if page != (event,):
            raise DependencyUnavailable("control-state rehearsal event page mismatch")
        payload = _rehearsal_report("collected")
        payload.update(_rehearsal_event_report(event, acknowledgement))
        if args.confirm_fence != "external-fence":
            _emit_rehearsal_report(args, payload)
            return 0

        attempt = coordinator.prepare_takeover(
            event.scope,
            event,
            new_owner_id=peer_id,
            acknowledgement=acknowledgement,
        )
    except DependencyUnavailable:
        _emit_rehearsal_report(
            args, _rehearsal_report("blocked", reason="dependency_unavailable")
        )
        return 1
    except Exception:
        _emit_rehearsal_report(
            args, _rehearsal_report("blocked", reason="internal_error")
        )
        return 1

    receipt = attempt.fence_receipt
    payload["fence"] = {
        "requested": True,
        "new_owner_id": peer_id,
        "receipt_id": receipt.receipt_id,
        "accepted": receipt.accepted,
        "external": receipt.external,
        "partition_state": receipt.partition_state.value,
        "decision": {
            "allowed": attempt.decision.allowed,
            "reason": attempt.decision.reason,
            "next_epoch": attempt.decision.next_epoch,
            "data_replica_count": attempt.decision.data_replica_count,
        },
    }
    if attempt.decision.allowed is not True:
        payload["status"] = "blocked"
        payload["reason"] = "takeover_evidence_denied"
        _emit_rehearsal_report(args, payload)
        return 1

    payload["status"] = "fence_evidence_collected"
    _emit_rehearsal_report(args, payload)
    return 0


def _run_preflight(config, *, check_ollama=True, ollama_timeout=5.0):
    """Resolve the host adapter only when an entry-point command needs it."""
    from .adapters.preflight_executor import PreflightExecutor
    from .application.preflight import PreflightService

    return PreflightService(PreflightExecutor()).run(
        config,
        check_ollama=check_ollama,
        ollama_timeout=ollama_timeout,
    )


def cmd_preflight(args) -> int:
    try:
        config = _load_config(args)
    except sonder_config.ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    report = _run_preflight(
        config, check_ollama=not args.skip_ollama
    )
    _emit(report.as_dict(), as_json=args.json)
    return 0 if report.ok else 1


def cmd_doctor(args) -> int:
    """Run the consolidated read-only health report."""
    import sonder_doctor
    from sonder_runtime.bootstrap.doctor_formatting import (
        STATUS_FAIL,
        render_report,
    )

    try:
        config = _load_config(args)
    except sonder_config.ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    checks = sonder_doctor.default_checks()
    replacements = dict(sonder_doctor.storage_checks(
        config, throughput=args.storage_probe,
        discover_models=not args.skip_ollama,
    ))
    replacements.update(sonder_doctor.ollama_checks(config))
    replacements.update(sonder_doctor.sonder_inference_checks())
    replacements["schemas"] = sonder_doctor.schema_check(config)
    replacements["schema_epoch"] = sonder_doctor.schema_epoch_check(config)
    replacements.update(sonder_doctor.memory_checks(config))
    replacements["backup"] = sonder_doctor.backup_check(config)
    checks = [
        (
            name,
            sonder_doctor.validated_config_check(config)
            if name == "config" and check is sonder_doctor._check_config
            else replacements.get(name, check),
        )
        for name, check in checks
    ]
    if args.skip_ollama:
        skipped_names = {"ollama", "ollama_workers", "ollama_residency"}
        checks = [
            (name, check) for name, check in checks if name not in skipped_names
        ]
    if args.skip_inference:
        skipped_names = {"sonder_inference", "sonder_inference_scope"}
        checks = [
            (name, check) for name, check in checks if name not in skipped_names
        ]
    report = sonder_doctor.run_doctor(checks)
    if args.json:
        _emit(report, as_json=True)
    else:
        print(render_report(report))
    return 1 if report.get("overall") == STATUS_FAIL else 0


def _warn_config_errors_reported(args) -> None:
    """Flag an invalid configuration on stderr for always-available reports.

    ``status`` and ``diagnostics`` deliberately still exit 0 and emit their
    payload when the configuration is invalid -- they are what an operator
    collects from a broken install -- but the failure must not be visible
    only inside the payload. stdout stays a parseable report.
    """
    print(
        "WARNING: configuration is invalid (see config_errors); this %s "
        "report still exits 0 -- use `config`, `doctor` or `preflight` "
        "(exit 2) to gate on configuration validity"
        % getattr(args, "command", "status"),
        file=sys.stderr,
    )


def cmd_status(args) -> int:
    import sonder_runtime.adapters.persistence.migrations as sonder_migrations

    build = sonder_version.build_info()
    payload: dict = {"build": build.as_dict()}
    try:
        config = _load_config(args)
        payload["profile"] = config.profile
        payload["config_sources"] = list(config.sources)
        _export_runtime_environment(config)
        try:
            from sonder_runtime.adapters import storage
            payload["storage"] = storage.inspect_config(config)
        except Exception as exc:  # status remains available on probe defects
            payload["storage_error"] = (
                "%s while inspecting storage (detail suppressed)"
                % exc.__class__.__name__
            )
    except sonder_config.ConfigError as exc:
        payload["config_errors"] = list(exc.errors)
        _warn_config_errors_reported(args)
    try:
        payload["schemas"] = {
            store: {
                "applied": len(status.applied),
                "pending": list(status.pending),
                "healthy": status.healthy,
            }
            for store, status in sonder_migrations.status_all().items()
        }
    except Exception as exc:
        payload["schemas_error"] = str(exc)
    _emit(payload, as_json=args.json)
    return 0


def cmd_diagnostics(args) -> int:
    import sonder_runtime.adapters.persistence.migrations as sonder_migrations

    payload: dict = {"build": sonder_version.build_info().as_dict()}
    try:
        config = _load_config(args)
        payload["config"] = config.as_redacted_dict()
        payload["preflight"] = _run_preflight(
            config, check_ollama=not args.skip_ollama
        ).as_dict()
        _export_runtime_environment(config)
    except sonder_config.ConfigError as exc:
        payload["config_errors"] = list(exc.errors)
        _warn_config_errors_reported(args)
    try:
        payload["schemas"] = {
            store: {
                "applied": list(status.applied),
                "pending": list(status.pending),
                "unknown": list(status.unknown),
                "checksum_mismatches": list(status.checksum_mismatches),
                "healthy": status.healthy,
            }
            for store, status in sonder_migrations.status_all().items()
        }
    except Exception as exc:
        payload["schemas_error"] = str(exc)
    _emit(payload, as_json=True)
    return 0


def cmd_config(args) -> int:
    try:
        config = _load_config(args)
    except sonder_config.ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    _emit(config.as_redacted_dict(), as_json=args.json)
    return 0


def _read_epoch2_receipt(home) -> dict:
    """Return the adoption receipt's fields, or ``{}`` when unreadable."""
    try:
        data = json.loads(
            (home / "epoch2_adoption_receipt.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def cmd_migrate(args) -> int:
    import sonder_runtime.adapters.persistence.migrations as sonder_migrations

    if getattr(args, "adopt_epoch2", False) and args.store:
        # --store used to be accepted and silently ignored here, so an
        # operator scoping the command to one store still adopted every one.
        print(
            "--store cannot be combined with --adopt-epoch2: epoch adoption "
            "always covers every domain database",
            file=sys.stderr,
        )
        return 2
    try:
        config = _load_config(args)
        _configure_typed_home(config)
        # Migration paths are environment-backed compatibility adapters; without
        # this process-local override --config/--set selected one home but
        # migrated another. Keep the compatibility export for the remaining
        # legacy settings, but path resolution is owned by the typed config.
        _export_runtime_environment(config)
        if getattr(args, "adopt_epoch2", False):
            from sonder_runtime.adapters.persistence.epoch_adoption import (
                check_epoch2_cleanup,
            )
            from sonder_runtime.adapters.persistence.sqlite.bridge_migration import (
                run_bridge_migration,
            )

            home = runtime_paths.default_home()
            # Re-running an already verified adoption used to take another
            # full pre-epoch2 copy every time. A home that already passes the
            # read-only adoption check is a verified no-op: nothing is copied
            # or rewritten. Partial/unverified state still re-runs the
            # crash-safe bridge, which backs up first.
            import sqlite3

            try:
                existing = (
                    check_epoch2_cleanup(home) if home.is_dir() else None
                )
            except (OSError, sqlite3.Error):
                existing = None  # not provably adopted: take the safe path
            if existing is not None and existing.allowed:
                prior = _read_epoch2_receipt(home)
                _emit(
                    {
                        "adopted": True,
                        "already_adopted": True,
                        "epoch": 2,
                        "source_version": prior.get("source_version"),
                        "backup_path": prior.get("backup_path"),
                        "tasks_migrated": 0,
                        "verified": True,
                    },
                    as_json=args.json,
                )
                return 0
            receipt = run_bridge_migration(home, version="spec5-bridge-cli")
            cleanup = check_epoch2_cleanup(home)
            if not cleanup.allowed:
                print(
                    "epoch-2 adoption completed but verification failed: "
                    + "; ".join(cleanup.reasons),
                    file=sys.stderr,
                )
                return 1
            _emit(
                {
                    "adopted": True,
                    "already_adopted": False,
                    "epoch": receipt.epoch,
                    "source_version": receipt.source_version,
                    "backup_path": receipt.backup_path,
                    "tasks_migrated": receipt.tasks_migrated,
                    "verified": cleanup.allowed,
                },
                as_json=args.json,
            )
            return 0
        if args.store:
            results = {
                args.store: sonder_migrations.migrate_store(args.store)
            }
        else:
            results = sonder_migrations.migrate_all()
    except sonder_migrations.MigrationError as exc:
        print(f"migration failed: {exc}", file=sys.stderr)
        return 1
    # `applied`/`pending` alone cannot distinguish "there was nothing to do"
    # from "this build has nothing to do it with". `discovered` and the two
    # health tuples are what let a caller tell those apart; the update engine
    # refuses an activation whose migrate step cannot show them.
    payload = {
        store: {
            "db_path": status.db_path,
            "applied": list(status.applied),
            "pending": list(status.pending),
            "unknown": list(status.unknown),
            "checksum_mismatches": list(status.checksum_mismatches),
            "discovered": len(sonder_migrations.discover_migrations(store)),
        }
        for store, status in results.items()
    }
    _emit(payload, as_json=args.json)
    return 0


def _backup_target(args, config=None) -> str:
    if args.target:
        return args.target
    config = config or _load_config(args)
    if config and config.backup.target:
        return config.backup.target
    return str(runtime_paths.default_home() / "backups")


def _report_problems(
    ok_message: str, problems: list, *, path: str, as_json: bool
) -> int:
    """Emit a verification verdict, honouring ``--json``.

    The verify/smoke branches used to print a bare sentence on every path.
    ``--json`` was declared on their parsers but never read, so the runbook's
    own verification gate (``backup verify <dir> --json``) was accepted and
    then answered with prose a caller's ``json.loads`` could not parse.
    """
    if as_json:
        _emit(
            {"path": path, "ok": not problems, "problems": list(problems)},
            as_json=True,
        )
        return 1 if problems else 0
    if problems:
        for problem in problems:
            print(f"FAIL: {problem}", file=sys.stderr)
        return 1
    print(ok_message)
    return 0


def _backup_failure(verb: str, exc: BaseException) -> int:
    """Report an expected backup/restore fault without a traceback."""
    detail = str(exc) or type(exc).__name__
    if isinstance(exc, OSError) and exc.strerror:
        detail = "%s: %s" % (exc.strerror, exc.filename or "")
        detail = detail.rstrip(": ")
    print("%s failed: %s" % (verb, detail), file=sys.stderr)
    return 1


def cmd_backup(args) -> int:
    from .adapters.backup import BackupError
    from .adapters.persistence.operations_store import MaintenanceLockHeld

    try:
        return _cmd_backup(args)
    except (BackupError, MaintenanceLockHeld, OSError) as exc:
        return _backup_failure("backup", exc)


def _cmd_backup(args) -> int:
    from .bootstrap.app import default_app

    config = None
    if args.backup_command != "verify":
        config = _load_config(args)
        if not config.backup.enabled:
            print("backups are disabled in configuration "
                  "([backup].enabled = false)", file=sys.stderr)
            return 1
        # Backup source discovery reads SONDER_HOME; exporting only the validated
        # target backed up unrelated state while reporting success.
        _export_runtime_environment(config)
        backups = default_app(config=config).backup
    else:
        backups = default_app().backup
    if args.backup_command == "create":
        result = backups.create(_backup_target(args, config))
        _emit(
            {
                "backup_id": result.backup_id,
                "path": str(result.path),
                "files": result.file_count,
                "total_bytes": result.total_bytes,
            },
            as_json=args.json,
        )
        return 0
    if args.backup_command == "verify":
        return _report_problems(
            "backup verified", backups.verify(args.path),
            path=args.path, as_json=args.json,
        )
    if args.backup_command == "list":
        _emit({"backups": backups.list(_backup_target(args, config))},
              as_json=args.json)
        return 0
    if args.backup_command == "latest":
        latest = backups.latest_dated(_backup_target(args, config))
        if latest is None:
            print("no backup with a valid created_at_utc in the target",
                  file=sys.stderr)
            return 1
        if args.json:
            _emit({"backup": latest}, as_json=True)
        else:
            # A bare path, so shell callers can use it without parsing JSON.
            print(latest["path"])
        return 0
    if args.backup_command == "prune":
        if args.keep is not None:
            removed = backups.prune(
                _backup_target(args, config), keep=args.keep
            )
        else:
            daily = config.backup.retention_daily
            weekly = config.backup.retention_weekly
            monthly = config.backup.retention_monthly
            removed = backups.prune_tiered(
                _backup_target(args, config), daily=daily, weekly=weekly,
                monthly=monthly,
            )
        _emit({"removed": removed}, as_json=args.json)
        return 0
    raise AssertionError(args.backup_command)


def cmd_restore(args) -> int:
    from .adapters.backup import BackupError
    from .adapters.persistence.operations_store import MaintenanceLockHeld

    try:
        return _cmd_restore(args)
    except (BackupError, MaintenanceLockHeld, OSError) as exc:
        return _backup_failure("restore", exc)


def _cmd_restore(args) -> int:
    if args.restore_command == "rehearse":
        # Filesystem-only: the drill needs no application graph, services
        # or state home, and must not open the live stores.
        return _restore_rehearse(args)

    from .bootstrap.app import default_app

    backups = default_app().backup

    if args.restore_command == "verify":
        return _report_problems(
            "backup verified", backups.verify(args.path),
            path=args.path, as_json=args.json,
        )
    if args.restore_command == "smoke":
        return _report_problems(
            "restore smoke passed", backups.smoke_restore(args.path),
            path=args.path, as_json=args.json,
        )
    if args.restore_command == "apply":
        if not args.confirm or args.confirm != "restore":
            print(
                "restore apply overwrites nothing but writes state files; "
                "pass --confirm restore to proceed",
                file=sys.stderr,
            )
            return 2
        restored = backups.restore_to_empty(args.path, args.destination)
        _emit({"restored": restored}, as_json=args.json)
        # --json promises one parseable document on stdout; keep the
        # operator hint visible without corrupting it.
        print(
            "State restored. Point SONDER_HOME at the destination (or move "
            "it into place with the service stopped) per "
            "docs/runbooks/backup-restore.md.",
            file=sys.stderr if args.json else sys.stdout,
        )
        return 0
    raise AssertionError(args.restore_command)


_REHEARSAL_TARGET_SUFFIX = "+rehearsal"


def _rehearsal_target_revision(source_revision: str, requested: str | None) -> str:
    """The candidate revision the scripted upgrade pretends to install.

    It only labels the disposable release marker, so a caller that has no real
    candidate gets a value that is visibly synthetic and always differs from
    the source revision the backup records.
    """
    if requested is not None:
        return requested
    from sonder_runtime.application.updates.recovery_rehearsal import MAX_REVISION_CHARS

    candidate = source_revision + _REHEARSAL_TARGET_SUFFIX
    if len(candidate) <= MAX_REVISION_CHARS:
        return candidate
    import hashlib

    return "rehearsal-of:" + hashlib.sha256(source_revision.encode("utf-8")).hexdigest()


def _restore_rehearse(args) -> int:
    """Run the P6 offline recovery rehearsal against one backup directory.

    Restores the backup into a fresh directory under a fenced workspace,
    applies a scripted candidate upgrade that fails on purpose, rolls it back,
    restores the authoritative state from the same backup, verifies every
    digest, and removes the disposable tree.  It never reads or switches the
    live release pointer or the running state home.
    """
    import tempfile
    import uuid

    from sonder_runtime.adapters.updates.offline_rehearsal import (
        FilesystemOfflineRecoveryPort,
    )
    from sonder_runtime.application.updates.recovery_rehearsal import (
        OfflineRecoveryRehearsal, OfflineRehearsalRequest, RehearsalError,
    )

    as_json = bool(args.json)
    owned_workspace = args.workspace is None
    if owned_workspace:
        workspace = tempfile.mkdtemp(prefix="sonder-recovery-rehearsal-")
    else:
        workspace = os.path.abspath(os.path.expanduser(args.workspace))

    def refuse(payload: dict, code: int) -> int:
        payload = {"ok": False, "path": args.path, "workspace": workspace, **payload}
        if as_json:
            _emit(payload, as_json=True)
        else:
            print("FAIL: %s: %s" % (payload["error"], payload["message"]), file=sys.stderr)
            if payload.get("steps_completed"):
                print("steps completed: " + ", ".join(payload["steps_completed"]),
                      file=sys.stderr)
        return code

    try:
        try:
            port = FilesystemOfflineRecoveryPort(workspace)
        except (OSError, ValueError) as exc:
            return refuse({
                "error": "workspace_invalid",
                "message": "--workspace must be an existing regular directory ("
                           + type(exc).__name__ + ")",
                "steps_completed": [],
            }, 2)
        if args.source_revision is not None:
            source_revision = args.source_revision
        else:
            try:
                source_revision = port.inspect_backup(args.path).source_revision
            except RehearsalError as exc:
                return refuse({
                    "error": type(exc).__name__, "message": str(exc),
                    "steps_completed": [],
                }, 1)
            except OSError as exc:
                return refuse({
                    "error": "backup_unreadable",
                    "message": "backup directory is unreadable (" + type(exc).__name__ + ")",
                    "steps_completed": [],
                }, 1)
        destination = os.path.join(workspace, "rehearsal-" + uuid.uuid4().hex[:12])
        try:
            request = OfflineRehearsalRequest(
                backup_ref=args.path,
                destination_ref=destination,
                source_revision=source_revision,
                target_revision=_rehearsal_target_revision(
                    source_revision, args.target_revision,
                ),
            )
        except ValueError as exc:
            return refuse({
                "error": "request_invalid", "message": str(exc), "steps_completed": [],
            }, 2)
        try:
            report = OfflineRecoveryRehearsal(port).run(request)
        except RehearsalError as exc:
            return refuse({
                "error": type(exc).__name__,
                "message": str(exc),
                "steps_completed": [step.value for step in exc.steps],
                "destination_left": os.path.lexists(destination),
            }, 1)
        payload = {"ok": True, "path": args.path, "workspace": workspace,
                   **report.as_dict(), "evidence_digest": report.evidence_digest}
        _emit(payload, as_json=as_json)
        if not as_json:
            print("Offline recovery rehearsal passed; the disposable tree was "
                  "removed and the live state was not touched.")
        return 0
    finally:
        if owned_workspace:
            # Only an empty directory this command created is removed: a
            # tree the rehearsal left behind (cleanup refused past its bound)
            # stays for the operator to inspect, as the contract requires.
            with contextlib.suppress(OSError):
                os.rmdir(workspace)


def cmd_smoke(args) -> int:
    """Minimal end-to-end: config, migrations, operations write/read."""
    import sonder_runtime.adapters.persistence.migrations as sonder_migrations
    from sonder_runtime.adapters.persistence.operations_store import OperationsStore

    try:
        config = _load_config(args)
    except sonder_config.ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    failures = []
    report = _run_preflight(config, check_ollama=not args.skip_ollama)
    if not report.ok:
        failures.extend(
            f"preflight: {c.name}: {c.detail}"
            for c in report.checks
            if c.required and not c.ok
        )
    # Smoke must exercise the state directory it just preflighted, not whatever
    # SONDER_HOME happened to be inherited by the process.
    _export_runtime_environment(config)
    try:
        sonder_migrations.migrate_store("operations")
        store = OperationsStore()
        event = store.record_event(
            component="smoke",
            event_code="SMOKE_EVENT",
            summary="smoke test event",
            detail={"pid": os.getpid()},
        )
        found = any(
            e.event_id == event.event_id for e in store.recent_events(limit=10)
        )
        if not found:
            failures.append("operations store roundtrip failed")
    except Exception as exc:
        failures.append(f"operations store: {exc}")
    if getattr(args, "json", False):
        _emit({"ok": not failures, "failures": failures}, as_json=True)
        return 1 if failures else 0
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1
    print("smoke passed")
    return 0


def _export_runtime_environment(config, *, include_typed_runtime: bool = True) -> None:
    """Publish the validated configuration through the compatibility vars.

    The stdlib HTTP adapter and the shared helper modules still read their
    environment, so anything not exported here is a validated setting the
    runtime never sees.  Four keys used to be preflighted and then dropped:
    ``state.home`` was created, write-probed and disk-checked while the
    runtime wrote to ``sonder_paths.default_home()`` instead;
    ``state.workspace_roots`` was a required startup check that granted the
    file tools no access; ``ollama.url`` was probed for reachability while
    the gateway dialled ``OLLAMA_HOST``; and the ``[features]`` consent
    gates read ``false`` in ``config`` output while web egress and live
    reload defaulted to on in the code that decides.
    """
    # sonder_paths resolves every state file through SONDER_HOME at call
    # time, so the home must be exported before anything opens a database.
    if config.state.home:
        os.environ["SONDER_HOME"] = config.state.home
    if config.state.workspace_roots:
        os.environ["SONDER_FILE_ROOTS"] = os.pathsep.join(
            config.state.workspace_roots
        )
    if include_typed_runtime:
        os.environ["SONDER_HOST"] = config.server.host
        os.environ["SONDER_PORT"] = str(config.server.port)
    # ``require_account`` predates the explicit mode field.  Preserve its
    # documented meaning when a configuration relies on the default api-key
    # mode rather than quietly letting the explicit default take precedence.
    auth_mode = config.server.auth_mode
    if config.server.require_account and auth_mode == "api-key":
        auth_mode = "account"
    if include_typed_runtime:
        os.environ["SONDER_AUTH_MODE"] = auth_mode
        os.environ["SONDER_MAX_REQUEST_BYTES"] = str(config.server.max_request_bytes)
        os.environ["SONDER_MAX_CONCURRENT_REQUESTS"] = str(
            config.server.max_concurrent_requests
        )
        os.environ["SONDER_REQUEST_TIMEOUT_SECONDS"] = str(
            config.server.request_timeout_seconds
        )
        os.environ["SONDER_STREAM_IDLE_TIMEOUT_SECONDS"] = str(
            config.server.stream_idle_timeout_seconds
        )
        os.environ["SONDER_CORS_ORIGINS"] = ",".join(config.server.cors_origins)
        os.environ["SONDER_REQUIRE_ACCOUNT"] = "1" if config.server.require_account else "0"
        os.environ["SONDER_ALLOW_REGISTRATION"] = (
            "1" if config.server.allow_registration else "0"
        )
        os.environ["SONDER_REASONING_AUDIENCE"] = config.server.reasoning_audience
        os.environ["SONDER_HTTP_SESSION_STATE_LIMIT"] = str(config.server.session_state_limit)
        os.environ["SONDER_HTTP_SESSION_STATE_OWNER_LIMIT"] = str(
            config.server.session_state_owner_limit
        )
        os.environ["SONDER_TRAIN_MAX_N"] = str(config.server.train_max_n)
    # The stdlib HTTP adapter has a final bind-time gate as well as config
    # validation.  Export the validated proxy declaration so a direct adapter
    # import cannot weaken a non-loopback deployment between those boundaries.
    # The preflight and legacy deployment probes still consume this explicit
    # proxy declaration before the HTTP adapter is fully active.
    os.environ["SONDER_TLS_TERMINATED_BY_PROXY"] = (
        "1" if config.server.tls_terminated_by_proxy else "0"
    )
    # Canonical ``serve`` binds the validated URL into the typed endpoint
    # adapter before lazy legacy providers are composed.  Keep exporting the
    # variable for compatibility subcommands, but do not recreate the mutable
    # environment bridge on the canonical path.
    # The worker list and consent flag must cross the canonical serve path so
    # TOML workers are not silently discarded before the lazy pool is built.
    # OLLAMA_HOST remains a legacy compatibility override and is exported only
    # for compatibility subcommands; canonical typed serve uses its bound
    # endpoint directly.
    if include_typed_runtime:
        os.environ["OLLAMA_HOST"] = config.ollama.url
        os.environ["SONDER_OLLAMA_POOL_MAX_WORKERS"] = str(
            config.ollama.worker_pool_max_workers
        )
        os.environ["SONDER_OLLAMA_WORKER_MAX_INFLIGHT"] = str(
            config.ollama.worker_max_inflight
        )
        os.environ["SONDER_OLLAMA_WORKER_QUEUE_DEPTH"] = str(
            config.ollama.worker_queue_depth
        )
        os.environ["SONDER_OLLAMA_WORKER_PROBE_PARALLELISM"] = str(
            config.ollama.worker_capability_probe_parallelism
        )
        os.environ["SONDER_OLLAMA_WORKER_PROBE_BATCH_SIZE"] = str(
            config.ollama.worker_capability_probe_batch_size
        )
        os.environ["SONDER_OLLAMA_WORKER_STATUS_PAGE_SIZE"] = str(
            config.ollama.worker_status_page_size
        )
        os.environ["SONDER_OLLAMA_WORKER_ADMISSION_TIMEOUT_MS"] = str(
            config.ollama.worker_admission_timeout_ms
        )
        os.environ["SONDER_OLLAMA_WORKER_FAILURE_THRESHOLD"] = str(
            config.ollama.worker_failure_threshold
        )
        os.environ["SONDER_OLLAMA_WORKER_COOLDOWN_SECONDS"] = str(
            config.ollama.worker_cooldown_seconds
        )
        os.environ["SONDER_OLLAMA_WORKER_CAPABILITY_TTL_SECONDS"] = str(
            config.ollama.worker_capability_ttl_seconds
        )
        os.environ["SONDER_OLLAMA_WORKER_PROBE_TIMEOUT_MS"] = str(
            config.ollama.worker_probe_timeout_ms
        )
    os.environ["SONDER_OLLAMA_WORKERS"] = ",".join(config.ollama.workers)
    os.environ["SONDER_ALLOW_REMOTE_OLLAMA"] = (
        "1" if config.ollama.allow_remote else "0"
    )
    os.environ["SONDER_TRUSTED_ORIGINS"] = ",".join(config.ollama.trusted_origins)
    os.environ["SONDER_ALLOW_CLOUD"] = "1" if config.features.cloud else "0"
    os.environ["SONDER_WEB_TOOLS"] = "1" if config.features.web else "0"
    os.environ["SONDER_LIVE_RELOAD"] = "1" if config.features.live_reload else "0"
    os.environ["SONDER_SOURCE_MODIFICATION"] = (
        "1" if config.features.source_modification else "0"
    )
    os.environ["SONDER_HOST_CONTROL"] = (
        "1" if config.features.host_control else "0"
    )
    os.environ["SONDER_TRAINING"] = "1" if config.features.training else "0"
    os.environ["SONDER_NPU"] = "1" if config.features.npu else "0"
    os.environ["SONDER_EXPOSE_REASONING"] = (
        "1" if config.features.expose_reasoning else "0"
    )
    os.environ["SONDER_ALLOW_PRIVATE_COT"] = (
        "1" if config.features.allow_private_cot else "0"
    )
    os.environ["SONDER_LOCATION_CONSENT"] = (
        "1" if config.features.location_consent else "0"
    )
    if include_typed_runtime:
        os.environ["SONDER_QUEUE_DEPTH"] = str(config.capacity.queue_depth)
        os.environ["SONDER_SPECULATION_SLOTS"] = str(
            config.capacity.model_generations
        )
        os.environ["SONDER_METRICS"] = "1" if config.observability.metrics_enabled else "0"
    if config.secrets.api_key:
        os.environ["SONDER_API_KEY"] = config.secrets.api_key
    if config.secrets.auth_secret:
        os.environ["SONDER_AUTH_SECRET"] = config.secrets.auth_secret


def _configure_typed_home(config) -> None:
    """Bind a non-empty typed state home without mutating ``os.environ``."""
    if config.state.home:
        runtime_paths.configure_home(config.state.home)


def cmd_serve(args) -> int:
    try:
        config = _load_config(args)
    except sonder_config.ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    # Export worker config to env early so the pool sees it even if
    # server.py is imported before configure_typed_workers runs.
    os.environ["SONDER_OLLAMA_WORKERS"] = ",".join(config.ollama.workers)
    os.environ["SONDER_ALLOW_REMOTE_OLLAMA"] = (
        "1" if config.ollama.allow_remote else "0"
    )
    os.environ["SONDER_TRUSTED_ORIGINS"] = ",".join(config.ollama.trusted_origins)
    from sonder_runtime.platform.logging import configure_logging
    configure_logging(
        level=config.observability.log_level,
        log_format=config.observability.log_format,
        redactor=_redactor_for_config(config),
    )
    if not args.skip_preflight:
        report = _run_preflight(
            config, check_ollama=not args.skip_ollama
        )
        if not report.ok:
            for check in report.checks:
                if check.required and not check.ok:
                    print(f"PREFLIGHT FAIL: {check.name}: {check.detail}",
                          file=sys.stderr)
            print("refusing to bind with failed preflight "
                  "(use --skip-preflight only for recovery work)",
                  file=sys.stderr)
            return 1
    # Bind non-HTTP typed state before migration/binding. The canonical HTTP
    # adapter publishes its artifact/control bindings inside serve.main's
    # ownership guard, after migrations have succeeded, so a refused migration
    # cannot leave a live route behind.
    _configure_typed_home(config)
    from sonder_runtime.adapters.inference import ollama_endpoint
    ollama_endpoint.configure_typed_endpoint(config.ollama.url)
    from sonder_runtime.adapters import embeddings as sonder_embeddings
    sonder_embeddings.configure_typed_endpoint(config.ollama.url)
    from sonder_runtime.adapters.inference import ollama_pool
    ollama_pool.configure_typed_workers(
        config.ollama.workers,
        allow_remote=config.ollama.allow_remote,
        trusted_origins=config.ollama.trusted_origins,
        failure_threshold=config.ollama.worker_failure_threshold,
        cooldown_seconds=config.ollama.worker_cooldown_seconds,
        admission_timeout_ms=config.ollama.worker_admission_timeout_ms,
        capability_ttl_seconds=config.ollama.worker_capability_ttl_seconds,
        probe_timeout_ms=config.ollama.worker_probe_timeout_ms,
        max_inflight_per_worker=config.ollama.worker_max_inflight,
        queue_depth=config.ollama.worker_queue_depth,
        max_workers=config.ollama.worker_pool_max_workers,
        capability_probe_parallelism=(
            config.ollama.worker_capability_probe_parallelism
        ),
        capability_probe_batch_size=config.ollama.worker_capability_probe_batch_size,
        status_page_size=config.ollama.worker_status_page_size,
    )
    from sonder_runtime.adapters.inference import ollama_vision
    ollama_vision.configure_typed_request_timeout(
        config.ollama.request_timeout_seconds,
    )
    from sonder_runtime.adapters.persistence.sqlite.bridge_migration import (
        require_epoch_2,
    )
    from sonder_runtime.domain.common.errors import MigrationRequired

    try:
        require_epoch_2(runtime_paths.default_home())
    except MigrationRequired as exc:
        print(
            f"migration required before serve: {exc}; "
            "run `migrate --adopt-epoch2`",
            file=sys.stderr,
        )
        return 1
    import sonder_runtime.interfaces.http.serve as sonder_serve
    _export_runtime_environment(config, include_typed_runtime=False)

    # MIGRATING phase: no listener opens until migrations complete.
    import sonder_runtime.adapters.persistence.migrations as sonder_migrations

    try:
        sonder_migrations.migrate_all(
            busy_timeout_ms=config.state.sqlite_busy_timeout_ms
        )
    except sonder_migrations.MigrationError as exc:
        print(f"migration failed, refusing to bind: {exc}", file=sys.stderr)
        return 1

    try:
        from sonder_runtime.adapters.persistence.operations_store import OperationsStore
        pruned = OperationsStore().prune_events(
            config.observability.audit_retention_days
        )
        if pruned:
            print(f"pruned {pruned} audit events older than "
                  f"{config.observability.audit_retention_days} days")
    except Exception:
        pass

    from sonder_runtime.bootstrap.legacy_interfaces import (
        configure_legacy_application,
        configure_legacy_interfaces,
        configure_legacy_capacity,
    )
    from sonder_runtime.bootstrap.app import (
        close_default_runtime_resources, default_app,
    )
    from sonder_runtime.interfaces.http.handlers import RecallHandler, OutcomeHandler
    # Compose typed admission before any boundary resolves the legacy root.
    app = default_app(config=config)
    try:
        configure_legacy_application(app)
        configure_legacy_interfaces()
        configure_legacy_capacity(
            autopilot_runs=config.capacity.autopilot_runs,
            fleet_workers=config.capacity.fleet_workers,
            training_jobs=config.capacity.training_jobs,
        )
        sonder_serve.configure_thin_handlers({
            "/v1/recall": RecallHandler(app.memory),
            "/v1/outcome": OutcomeHandler(app.memory),
        })
        sys.argv = ["python -m sonder_runtime serve", str(config.server.port)]
        sonder_serve.main(config=config, _close_default_resources=False)
    finally:
        close_default_runtime_resources(timeout=5)
    return 0


def _repl_is_interactive(*, machine_output: bool, stdin=None, stdout=None) -> bool:
    if machine_output:
        return False
    for stream in (stdin or sys.stdin, stdout or sys.stdout):
        try:
            if not stream.isatty():
                return False
        except (AttributeError, OSError, ValueError):
            return False
    return True


def _configure_repl_logging(config, *, machine_output: bool, stdin=None, stdout=None):
    """Keep JSON log lines off the interactive REPL (serve/MCP unchanged).

    Records go to ``SONDER_HOME/logs/repl.log``; on a terminal, WARNING+ is
    queued in ``application.ports.repl_notices`` for the REPL to drain between
    turns. ``SONDER_REPL_LOG_STDERR=1`` restores JSON on stderr.
    """
    from sonder_runtime.application.ports import repl_notices
    from sonder_runtime.platform.logging import configure_repl_logging

    interactive = _repl_is_interactive(
        machine_output=machine_output, stdin=stdin, stdout=stdout,
    )
    queue = repl_notices.ReplNoticeQueue() if interactive else None
    plan = configure_repl_logging(
        home=runtime_paths.default_home(),
        interactive=interactive,
        notice_sink=None if queue is None else queue.push,
        redactor=_redactor_for_config(config),
        log_format=config.observability.log_format,
    )
    repl_notices.install_repl_notices(
        queue if plan.console == "notices" else None,
        log_path=plan.file_path,
    )
    return plan


def cmd_repl(args) -> int:
    try:
        config = _load_config(args)
    except sonder_config.ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    _configure_typed_home(config)
    _configure_repl_logging(config, machine_output=bool(args.json))
    _export_runtime_environment(config)
    import sonder_runtime.adapters.persistence.migrations as sonder_migrations
    try:
        sonder_migrations.migrate_all(
            busy_timeout_ms=config.state.sqlite_busy_timeout_ms
        )
    except sonder_migrations.MigrationError as exc:
        print(f"migration failed: {exc}", file=sys.stderr)
        return 1
    import sonder_runtime.interfaces.repl.repl as sonder_repl
    from sonder_runtime.bootstrap.legacy_interfaces import configure_legacy_interfaces

    configure_legacy_interfaces()
    from sonder_runtime.bootstrap.app import (
        close_default_runtime_resources, default_app,
    )
    from sonder_runtime.bootstrap.legacy_interfaces import configure_legacy_application

    owned_application = None
    try:
        owned_application = default_app(config=config)
        configure_legacy_application(owned_application)
        if args.json:
            sonder_repl.run_jsonl()
        else:
            sonder_repl.main()
    finally:
        if owned_application is not None:
            close_default_runtime_resources(timeout=5)
    return 0


def cmd_mcp(args) -> int:
    if getattr(args, "progressive_tools", False) and not getattr(args, "native", False):
        print("--progressive-tools requires --native", file=sys.stderr)
        return 2
    if getattr(args, "native", False):
        from sonder_runtime.adapters.security import unsafe_lab

        try:
            unsafe_lab.require_startup()
        except unsafe_lab.UnsafeLabError as exc:
            print(f"native MCP startup refused: {exc}", file=sys.stderr)
            return 2
        try:
            config = _load_config(args)
        except sonder_config.ConfigError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        from sonder_runtime.bootstrap.app import build_application
        from sonder_runtime.bootstrap.native_mcp import run_native_mcp

        _configure_typed_home(config)
        from sonder_runtime.platform.logging import configure_logging
        configure_logging(
            level=config.observability.log_level,
            log_format=config.observability.log_format,
            redactor=_redactor_for_config(config),
        )
        _export_runtime_environment(config, include_typed_runtime=False)
        import sonder_runtime.adapters.persistence.migrations as sonder_migrations
        try:
            sonder_migrations.migrate_all(
                busy_timeout_ms=config.state.sqlite_busy_timeout_ms
            )
        except sonder_migrations.MigrationError as exc:
            print(f"migration failed: {exc}", file=sys.stderr)
            return 1
        application = build_application(config=config)
        try:
            # run_native_mcp returns the number of frames served, not a
            # status: a session that reached EOF cleanly exits 0.
            if getattr(args, "progressive_tools", False):
                run_native_mcp(application, close_compute_on_exit=False, progressive_tools=True)
            else:
                run_native_mcp(application, close_compute_on_exit=False)
            return 0
        finally:
            application.close_providers(timeout=5)
    from sonder_runtime.bootstrap.app import close_default_runtime_resources

    owned_application = None
    def _configure_mcp_legacy() -> None:
        nonlocal owned_application
        config = _load_config(args)
        _configure_typed_home(config)
        from sonder_runtime.platform.logging import configure_logging
        configure_logging(
            level=config.observability.log_level,
            log_format=config.observability.log_format,
            redactor=_redactor_for_config(config),
        )
        _export_runtime_environment(config)
        from sonder_runtime.bootstrap.app import default_app
        from sonder_runtime.bootstrap.legacy_mcp import configure_legacy_application
        from sonder_runtime.adapters.inference import ollama_endpoint
        ollama_endpoint.configure_typed_endpoint(config.ollama.url)
        from sonder_runtime.adapters.inference import ollama_pool
        ollama_pool.configure_typed_workers(
            config.ollama.workers,
            allow_remote=config.ollama.allow_remote,
            trusted_origins=config.ollama.trusted_origins,
            failure_threshold=config.ollama.worker_failure_threshold,
            cooldown_seconds=config.ollama.worker_cooldown_seconds,
            admission_timeout_ms=config.ollama.worker_admission_timeout_ms,
            capability_ttl_seconds=config.ollama.worker_capability_ttl_seconds,
            probe_timeout_ms=config.ollama.worker_probe_timeout_ms,
            max_inflight_per_worker=config.ollama.worker_max_inflight,
            queue_depth=config.ollama.worker_queue_depth,
            max_workers=config.ollama.worker_pool_max_workers,
            capability_probe_parallelism=(
                config.ollama.worker_capability_probe_parallelism
            ),
            capability_probe_batch_size=config.ollama.worker_capability_probe_batch_size,
            status_page_size=config.ollama.worker_status_page_size,
        )
        # Do not reset typed worker configuration after the application binds
        # its pool: that would erase the shared compatibility pool reference.
        owned_application = default_app(config=config)
        configure_legacy_application(owned_application)

    from sonder_runtime.adapters.security import unsafe_lab

    try:
        McpCommand(build_legacy_server_mcp_runtime()).execute(_configure_mcp_legacy)
    except sonder_config.ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except unsafe_lab.UnsafeLabError as exc:
        # Same refusal contract as --native: a clean message and exit 2, not
        # a traceback and exit 1. The gate still runs before any adapter.
        print(f"MCP startup refused: {exc}", file=sys.stderr)
        return 2
    finally:
        if owned_application is not None:
            close_default_runtime_resources(timeout=5)
    return 0


def cmd_drain(args) -> int:
    """Request graceful drain via POST /v1/admin/drain."""
    import urllib.error
    import urllib.request
    import uuid

    try:
        config = _load_config(args)
    except sonder_config.ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    url = f"http://127.0.0.1:{config.server.port}/v1/admin/drain"
    request = urllib.request.Request(url, method="POST", data=b"")
    if config.secrets.api_key:
        request.add_header("Authorization", f"Bearer {config.secrets.api_key}")
    request.add_header("Idempotency-Key", f"drain-{uuid.uuid4().hex}")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        print(f"drain request rejected: HTTP {exc.code}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as exc:
        print(f"cannot reach the runtime on port {config.server.port}: {exc}",
              file=sys.stderr)
        return 1
    _emit(payload, as_json=args.json)
    return 0


def cmd_update(args) -> int:
    import sonder_runtime.adapters.updates.engine as sonder_update_engine
    import sonder_runtime.adapters.updates.service as sonder_updates

    manager = sonder_update_engine.UpdateManager()
    try:
        if args.update_command == "status":
            _emit(manager.status(), as_json=True)
            return 0
        if args.update_command == "build":
            result = sonder_updates.build_bundle(
                args.source, args.output,
                version=args.bundle_version,
                channel=args.channel,
            )
            _emit(result, as_json=args.json)
            return 0
        if args.update_command == "import":
            plan = manager.import_offline(
                args.path,
                channel=args.channel,
                allow_unverified=args.allow_unverified,
                idempotency_key=args.idempotency_key,
            )
            payload = dict(plan)
            if plan["status"] == "available":
                payload["confirm_nonce"] = (
                    sonder_update_engine.confirm_nonce_for(plan)
                )
            _emit(payload, as_json=args.json)
            return 0 if plan["status"] == "available" else 1
        if args.update_command == "install":
            plan = manager.install(
                args.update_id,
                confirm=args.confirm or "",
                allow_unverified=args.allow_unverified,
                skip_backup=args.skip_backup,
            )
            _emit(dict(plan), as_json=args.json)
            return 0 if plan["status"] == "committed" else 1
        if args.update_command == "rollback":
            active = manager.rollback(confirm=args.confirm or "")
            _emit(dict(active), as_json=args.json)
            print(
                "Rollback complete. Restart the service to run the restored "
                "release.",
                file=sys.stderr if args.json else sys.stdout,
            )
            return 0
        if args.update_command == "cancel":
            plan = manager.cancel(args.update_id)
            _emit(dict(plan), as_json=args.json)
            return 0
        raise AssertionError(args.update_command)
    except sonder_updates.UpdateError as exc:
        print(f"update failed: {exc}", file=sys.stderr)
        return 1


def cmd_rotate_key(args) -> int:
    import sonder_runtime.adapters.secrets as sonder_secrets

    # Resolve the secrets file exactly like every other command does:
    # --secrets, then SONDER_SECRETS, then <state home>/sonder.env. Only
    # --secrets used to count, so an operator whose runtime reads its key via
    # SONDER_SECRETS was refused.
    secrets_path = _configured_path(args.secrets, "SONDER_SECRETS", "sonder.env")
    if not secrets_path:
        print(
            "rotate-key requires --secrets <path> (or SONDER_SECRETS, or a "
            "sonder.env in the state home)",
            file=sys.stderr,
        )
        return 2
    # --config/--set select the state home that receives the rotation state
    # and the audit event; both used to land in the environment's home. The
    # configuration is validated exactly as serve validates it (including the
    # secrets file), so a key is never rotated under a configuration the
    # restarted server would reject.
    try:
        config = _load_config(args)
    except sonder_config.ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    _configure_typed_home(config)
    try:
        report = sonder_secrets.rotate_api_key(
            secrets_path, overlap_seconds=args.overlap_seconds
        )
    except sonder_secrets.RotationError as exc:
        print(f"rotation failed: {exc}", file=sys.stderr)
        return 1
    from sonder_runtime.adapters.persistence.operations_store import OperationsStore

    try:
        OperationsStore().record_event(
            component="secrets",
            event_code="API_KEY_ROTATED",
            summary="API key rotated",
            detail={
                "previous_accepted_until": report["previous_accepted_until"]
            },
        )
    except Exception:
        print("WARNING: key rotation succeeded but audit record "
              "could not be written", file=sys.stderr)
    _emit(report, as_json=args.json)
    return 0


def cmd_eval_history(args) -> int:
    """Inspect history or explicitly append already-computed evidence.

    This command never runs an evaluation and never calls a model.
    """
    from .adapters import evaluation_history_store as eval_history

    try:
        if args.eval_history_command == "status":
            payload = eval_history.history_status(
                args.history,
                model=args.model,
                model_digest=args.model_digest,
                suite=args.suite,
                suite_version=args.suite_version,
                suite_digest=args.suite_digest,
                tolerance=args.tolerance,
            )
        else:
            payload = eval_history.record_result(
                args.history,
                model=args.model,
                model_digest=args.model_digest,
                suite=args.suite,
                suite_version=args.suite_version,
                suite_digest=args.suite_digest,
                passed=args.passed,
                total=args.total,
                recorded_at=args.recorded_at,
                source=args.source,
            )
    except (eval_history.HistoryError, OSError, TimeoutError) as exc:
        print("evaluation history error: %s" % exc, file=sys.stderr)
        return 2
    _emit(payload, as_json=args.json)
    return 0


def cmd_artifact_mobility(args) -> int:
    """Local operator-only adapter; never registered as a model-facing tool."""
    from sonder_runtime.bootstrap.app import _artifact_mobility_operator_application
    from sonder_runtime.bootstrap.artifact_mobility import mobility_error_projection
    try:
        application = _artifact_mobility_operator_application()
        if args.mobility_command == "status":
            payload = application.artifact_mobility_status(args.operation_id)
        elif args.mobility_command == "list":
            payload = application.artifact_mobility_list()
        elif args.mobility_command == "send":
            payload = application._artifact_mobility_binding().send(
                args.source_artifact, confirm_destination=args.confirm_destination)
        elif args.mobility_command == "resume":
            payload = application._artifact_mobility_binding().resume(args.operation_id)
        else:
            payload = {"outcome_code": "INVALID_REQUEST"}
        _emit(payload, as_json=args.json)
        return 2 if set(payload) == {"outcome_code"} else 0
    except Exception as error:
        _emit(mobility_error_projection(error), as_json=args.json)
        return 2


class _MobilityArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's normal error echoes rejected arguments, including URLs.
        self.exit(2, "artifact-mobility: INVALID_REQUEST\n")


def _check_mobility_arguments(values, parser):
    """Admit the complete local grammar before help or host composition."""
    action_options = {
        "send": {"--source-artifact", "--confirm-destination"},
        "resume": {"--operation-id"},
        "status": {"--operation-id"},
        "list": set(),
    }
    action = None
    index = 0
    while index < len(values):
        token = values[index]
        index += 1
        if token in ("--help", "-h"):
            continue
        if action is None and token in action_options:
            action = token
            continue
        if action is not None and token == "--json":
            continue
        option, separator, value = token.partition("=")
        if action is None or option not in action_options[action]:
            parser.error(None)
        if not separator:
            if index == len(values) or values[index].startswith("-"):
                parser.error(None)
            value = values[index]
            index += 1
        if option in ("--source-artifact", "--operation-id"):
            # IDs have exactly the store's bounded ASCII opaque-ID grammar.
            # Check length first; never normalize or echo a rejected value.
            if len(value) != 32 or any(char not in "0123456789abcdef" for char in value):
                parser.error(None)
        elif option == "--confirm-destination" and not is_public_mobility_label(value):
            parser.error(None)


class _ProductionArgumentParser(argparse.ArgumentParser):
    def parse_args(self, args=None, namespace=None):
        values = list(sys.argv[1:] if args is None else args)
        if any(value.casefold() == "artifact-mobility" for value in values):
            # Detect case variants before ordinary parsing can echo an earlier
            # misplaced value. Only exact lowercase syntax is admitted below.
            parser = _MobilityArgumentParser(prog="artifact-mobility", allow_abbrev=False)
            if not values or values[0] != "artifact-mobility":
                parser.error(None)
            _check_mobility_arguments(values[1:], parser)
            _add_mobility_arguments(parser)
            result = parser.parse_args(values[1:], namespace)
            result.command = "artifact-mobility"
            return result
        return super().parse_args(values, namespace)


def _add_mobility_arguments(parser):
    mobility_sub = parser.add_subparsers(dest="mobility_command", required=True,
        parser_class=_MobilityArgumentParser)
    for action in ("send", "resume", "status", "list"):
        mp = mobility_sub.add_parser(action, allow_abbrev=False)
        mp.add_argument("--json", action="store_true")
        if action == "send":
            mp.add_argument("--source-artifact", required=True)
            mp.add_argument("--confirm-destination", required=True)
        elif action in ("resume", "status"):
            mp.add_argument("--operation-id", required=True)
        mp.set_defaults(func=cmd_artifact_mobility)


def build_parser() -> argparse.ArgumentParser:
    parser = _ProductionArgumentParser(
        prog="python -m sonder_runtime",
        description="Sonder runtime production entry point",
    )
    parser.add_argument(
        "--version", action="version",
        version=f"sonder-runtime {sonder_version.build_info().version}",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p, *, ollama_flag: bool = False, json_help: str = "JSON output"):
        p.add_argument("--config", help="path to sonder.toml")
        p.add_argument("--secrets", help="path to the secrets env file")
        p.add_argument(
            "--set", action="append", metavar="SECTION.KEY=VALUE",
            help="explicit configuration override (highest precedence)",
        )
        p.add_argument("--json", action="store_true", help=json_help)
        if ollama_flag:
            p.add_argument(
                "--skip-ollama", action="store_true",
                help="do not probe the Ollama endpoint",
            )

    p = sub.add_parser("preflight", help="run startup checks, do not bind")
    common(p, ollama_flag=True)
    p.set_defaults(func=cmd_preflight)

    p = sub.add_parser("doctor", help="consolidated read-only health report")
    common(p)
    p.add_argument(
        "--skip-ollama", action="store_true",
        help="do not probe the Ollama endpoint",
    )
    p.add_argument(
        "--skip-inference", action="store_true",
        help="do not probe the Sonder Inference endpoint",
    )
    p.add_argument(
        "--storage-probe", action="store_true",
        help="explicitly run an 8 MiB/5 second state-storage throughput probe",
    )
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("status", help="local build/config/schema status")
    common(p)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("diagnostics", help="redacted diagnostic bundle")
    common(p, ollama_flag=True)
    p.set_defaults(func=cmd_diagnostics)

    p = sub.add_parser("config", help="show effective redacted configuration")
    common(p)
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("migrate", help="apply pending schema migrations")
    common(p)
    p.add_argument("--store", choices=STORE_NAMES)
    p.add_argument(
        "--adopt-epoch2", action="store_true",
        help="run the explicit crash-safe SPEC-5 epoch-2 bridge adoption",
    )
    p.set_defaults(func=cmd_migrate)

    p = sub.add_parser("backup", help="backup management")
    backup_sub = p.add_subparsers(dest="backup_command", required=True)
    for name in ("create", "list", "latest", "prune"):
        bp = backup_sub.add_parser(
            name,
            help=(
                "print the path of the newest backup with a valid "
                "created_at_utc (exit 1 when there is none)"
                if name == "latest" else None
            ),
        )
        common(bp)
        bp.add_argument("--target", help="backup repository directory")
        if name == "prune":
            bp.add_argument(
                "--keep", type=int, default=None,
                help="simple keep-N; omit for tiered daily/weekly/monthly",
            )
    bp = backup_sub.add_parser("verify")
    bp.add_argument("path")
    bp.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("restore", help="restore management")
    restore_sub = p.add_subparsers(dest="restore_command", required=True)
    rp = restore_sub.add_parser("verify")
    rp.add_argument("path")
    rp.add_argument("--json", action="store_true")
    rp = restore_sub.add_parser("smoke")
    rp.add_argument("path")
    rp.add_argument("--json", action="store_true")
    rp = restore_sub.add_parser("apply")
    rp.add_argument("path")
    rp.add_argument("destination")
    rp.add_argument("--confirm", help="pass 'restore' to confirm")
    rp.add_argument("--json", action="store_true")
    rp = restore_sub.add_parser(
        "rehearse",
        help="offline restore + failed-upgrade rollback drill in a disposable tree",
    )
    rp.add_argument("path", help="verified backup directory")
    rp.add_argument(
        "--workspace",
        help="existing directory that fences the disposable tree "
             "(default: a fresh temporary directory, removed afterwards)",
    )
    rp.add_argument(
        "--source-revision",
        help="revision the backup must record (default: the backup's own)",
    )
    rp.add_argument(
        "--target-revision",
        help="label for the scripted candidate upgrade "
             "(default: '<source>+rehearsal')",
    )
    rp.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("smoke", help="minimal end-to-end check")
    common(p, ollama_flag=True)
    p.set_defaults(func=cmd_smoke)

    p = sub.add_parser(
        "control-state-rehearsal",
        help="collect disposable external control-state evidence without promotion",
    )
    p.add_argument("--config", required=True, help="path to rehearsal-only sonder.toml")
    p.add_argument("--secrets", help="path to the rehearsal secrets env file")
    p.add_argument("--json", action="store_true", help="emit a redacted JSON report")
    p.add_argument("--event-id", required=True)
    p.add_argument("--resource-kind", required=True, choices=("job",))
    p.add_argument("--resource-id", required=True)
    p.add_argument("--owner-epoch", required=True, type=int)
    p.add_argument("--sequence", required=True, type=int)
    p.add_argument("--payload-digest", required=True)
    p.add_argument(
        "--confirm-fence",
        help="pass exactly external-fence before requesting one external receipt",
    )
    p.add_argument("--new-owner-id", help="must be the configured rehearsal peer")
    p.set_defaults(func=cmd_control_state_rehearsal)

    p = sub.add_parser("serve", help="run the HTTP adapter")
    common(p, ollama_flag=True)
    p.add_argument("port", nargs="?", type=int)
    p.add_argument("--skip-preflight", action="store_true")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("repl", help="run the interactive REPL")
    common(
        p,
        json_help=(
            "emit stdout as sonder.repl-output.v1 JSON Lines without terminal chrome"
        ),
    )
    p.set_defaults(func=cmd_repl)

    p = sub.add_parser("mcp", help="run the MCP adapter")
    common(p)
    p.add_argument(
        "--native", action="store_true",
        help="use the application-owned bounded MCP transport",
    )
    p.add_argument(
        "--progressive-tools", action="store_true",
        help="with --native, expose tool search and load selected schemas on demand",
    )
    p.set_defaults(func=cmd_mcp)

    p = sub.add_parser("drain", help="request graceful drain")
    common(p)
    p.set_defaults(func=cmd_drain)

    p = sub.add_parser("update", help="signed engine updates (SPEC-4)")
    update_sub = p.add_subparsers(dest="update_command", required=True)
    up = update_sub.add_parser("status")
    up.add_argument("--json", action="store_true")
    up = update_sub.add_parser("build", help="build a release bundle")
    up.add_argument("source")
    up.add_argument("output")
    up.add_argument("--bundle-version", default=None)
    up.add_argument("--channel", default="stable")
    up.add_argument("--json", action="store_true")
    up = update_sub.add_parser("import", help="import an offline bundle")
    up.add_argument("path")
    up.add_argument("--channel", default="stable")
    up.add_argument("--allow-unverified", action="store_true",
                    help="accept a bundle without TUF metadata (requires "
                         "SONDER_UPDATE_ALLOW_UNSIGNED=1; never production)")
    up.add_argument("--idempotency-key", default=None)
    up.add_argument("--json", action="store_true")
    up = update_sub.add_parser("install", help="install an imported update")
    up.add_argument("update_id")
    up.add_argument("--confirm", help="confirmation nonce from update status")
    up.add_argument("--allow-unverified", action="store_true")
    up.add_argument("--skip-backup", action="store_true",
                    help="skip the pre-install backup (testing only)")
    up.add_argument("--json", action="store_true")
    up = update_sub.add_parser("rollback", help="roll back to the previous release")
    up.add_argument("--confirm", help="last 8 chars of the previous release id")
    up.add_argument("--json", action="store_true")
    up = update_sub.add_parser("cancel")
    up.add_argument("update_id")
    up.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_update)

    p = sub.add_parser(
        "rotate-key", help="rotate SONDER_API_KEY with an overlap window"
    )
    common(p)
    p.add_argument(
        "--overlap-seconds", type=int, default=24 * 3600,
        help="how long the previous key stays valid (default 24h)",
    )
    p.set_defaults(func=cmd_rotate_key)

    p = sub.add_parser(
        "eval-history",
        help="inspect or explicitly record precomputed evaluation evidence",
    )
    history_sub = p.add_subparsers(
        dest="eval_history_command", required=True,
    )
    hp = history_sub.add_parser("status", help="read identity-separated trends")
    hp.add_argument("--history", help="history JSONL path")
    hp.add_argument("--model", default="")
    hp.add_argument("--model-digest", default="")
    hp.add_argument("--suite", default="")
    hp.add_argument("--suite-version", default="")
    hp.add_argument("--suite-digest", default="")
    hp.add_argument("--tolerance", type=float, default=0.0)
    hp.add_argument("--json", action="store_true")
    hp.set_defaults(func=cmd_eval_history)
    hp = history_sub.add_parser(
        "record", help="append one precomputed aggregate result (never runs a model)"
    )
    hp.add_argument("--history", help="history JSONL path")
    hp.add_argument("--model", required=True)
    hp.add_argument("--model-digest", required=True)
    hp.add_argument("--suite", required=True)
    hp.add_argument("--suite-version", required=True)
    hp.add_argument("--suite-digest", required=True)
    hp.add_argument("--passed", type=int, required=True)
    hp.add_argument("--total", type=int, required=True)
    hp.add_argument("--recorded-at", type=float, default=None)
    hp.add_argument("--source", default="manual")
    hp.add_argument("--json", action="store_true")
    hp.set_defaults(func=cmd_eval_history)

    p = sub.add_parser("artifact-mobility", help="explicit local fixed-peer artifact copy")
    p.error = lambda message: parser.exit(2, "artifact-mobility: INVALID_REQUEST\n")
    _add_mobility_arguments(p)

    return parser


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if _contains_rehearsal_literal(raw_argv):
        # argparse includes caller-provided values in its errors.  This one
        # command admits provider credentials and an explicitly bounded
        # disposable scope, so keep any parse failure containing its literal
        # content-free. A malformed leading option cannot safely distinguish
        # its value from a command token. Normal argv without this literal
        # retains ordinary argparse behavior. Nonzero parser exits are handled;
        # --help and --version retain their normal successful SystemExit behavior.
        with contextlib.redirect_stderr(io.StringIO()):
            try:
                args = build_parser().parse_args(raw_argv)
            except SystemExit as exc:
                if exc.code == 2:
                    _emit(
                        _rehearsal_report("rejected", reason="invalid_arguments"),
                        as_json="--json" in raw_argv,
                    )
                    return 2
                raise
    else:
        args = build_parser().parse_args(raw_argv)
    try:
        return args.func(args)
    except sonder_config.ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
