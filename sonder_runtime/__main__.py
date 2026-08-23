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
    restore       verify / apply a backup into an empty directory
    drain         request graceful drain of a running server
    smoke         minimal end-to-end check without a real model
    eval-history  inspect or explicitly record precomputed evaluation evidence
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from sonder_runtime.platform import config as sonder_config
from sonder_runtime.platform import paths as runtime_paths
from sonder_runtime.platform import version as sonder_version
from sonder_runtime.application.command_surface import McpCommand
from sonder_runtime.bootstrap.legacy_mcp import build_legacy_server_mcp_runtime


def _load_config(args) -> "sonder_config.SonderConfig":
    overrides = {}
    for item in getattr(args, "set", None) or []:
        if "=" not in item:
            raise sonder_config.ConfigError(
                [f"--set expects section.key=value, got {item!r}"]
            )
        key, _, value = item.partition("=")
        overrides[key.strip()] = value.strip()
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
        config, throughput=args.storage_probe
    ))
    replacements["schemas"] = sonder_doctor.schema_check(config)
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
    report = sonder_doctor.run_doctor(checks)
    if args.json:
        _emit(report, as_json=True)
    else:
        print(render_report(report))
    return 1 if report.get("overall") == STATUS_FAIL else 0


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


def cmd_migrate(args) -> int:
    import sonder_runtime.adapters.persistence.migrations as sonder_migrations

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


def cmd_backup(args) -> int:
    from .bootstrap.app import default_app

    config = None
    if args.backup_command != "verify":
        config = _load_config(args)
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
        print(
            "State restored. Point SONDER_HOME at the destination (or move "
            "it into place with the service stopped) per "
            "docs/runbooks/backup-restore.md.",
        )
        return 0
    raise AssertionError(args.restore_command)


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
    if include_typed_runtime:
        os.environ["OLLAMA_HOST"] = config.ollama.url
        os.environ["SONDER_OLLAMA_WORKERS"] = ",".join(config.ollama.workers)
    os.environ["SONDER_ALLOW_REMOTE_OLLAMA"] = (
        "1" if config.ollama.allow_remote else "0"
    )
    os.environ["SONDER_WEB_TOOLS"] = "1" if config.features.web else "0"
    os.environ["SONDER_LIVE_RELOAD"] = "1" if config.features.live_reload else "0"
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
    # Bind typed state and HTTP settings before migration/binding. The
    # compatibility export below is restricted to settings still consumed by
    # legacy adapters; typed HTTP authority no longer depends on environment
    # round-tripping.
    _configure_typed_home(config)
    from sonder_runtime.adapters.inference import ollama_endpoint
    ollama_endpoint.configure_typed_endpoint(config.ollama.url)
    from sonder_runtime.adapters.inference import ollama_pool
    ollama_pool.configure_typed_workers(
        config.ollama.workers, allow_remote=config.ollama.allow_remote,
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
    sonder_serve.configure_typed_config(config)
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

    from sonder_runtime.bootstrap.legacy_interfaces import configure_legacy_interfaces

    configure_legacy_interfaces()
    sys.argv = ["python -m sonder_runtime serve", str(config.server.port)]
    sonder_serve.main(config=config)
    return 0


def cmd_repl(args) -> int:
    try:
        _export_runtime_environment(_load_config(args))
    except sonder_config.ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    import sonder_runtime.interfaces.repl.repl as sonder_repl
    from sonder_runtime.bootstrap.legacy_interfaces import configure_legacy_interfaces

    configure_legacy_interfaces()
    sonder_repl.main()
    return 0


def cmd_mcp(args) -> int:
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
        _export_runtime_environment(config, include_typed_runtime=False)
        return run_native_mcp(build_application(config=config))
    try:
        McpCommand(build_legacy_server_mcp_runtime()).execute(
            lambda: _export_runtime_environment(_load_config(args))
        )
    except sonder_config.ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
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

    if not args.secrets:
        print("rotate-key requires --secrets <path>", file=sys.stderr)
        return 2
    try:
        report = sonder_secrets.rotate_api_key(
            args.secrets, overlap_seconds=args.overlap_seconds
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
        pass
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m sonder_runtime",
        description="Sonder runtime production entry point",
    )
    parser.add_argument(
        "--version", action="version",
        version=f"sonder-runtime {sonder_version.build_info().version}",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p, *, ollama_flag: bool = False):
        p.add_argument("--config", help="path to sonder.toml")
        p.add_argument("--secrets", help="path to the secrets env file")
        p.add_argument(
            "--set", action="append", metavar="SECTION.KEY=VALUE",
            help="explicit configuration override (highest precedence)",
        )
        p.add_argument("--json", action="store_true", help="JSON output")
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
    p.add_argument("--store", choices=("memory", "autopilot", "fleet", "operations"))
    p.add_argument(
        "--adopt-epoch2", action="store_true",
        help="run the explicit crash-safe SPEC-5 epoch-2 bridge adoption",
    )
    p.set_defaults(func=cmd_migrate)

    p = sub.add_parser("backup", help="backup management")
    backup_sub = p.add_subparsers(dest="backup_command", required=True)
    for name in ("create", "list", "prune"):
        bp = backup_sub.add_parser(name)
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
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("smoke", help="minimal end-to-end check")
    common(p, ollama_flag=True)
    p.set_defaults(func=cmd_smoke)

    p = sub.add_parser("serve", help="run the HTTP adapter")
    common(p, ollama_flag=True)
    p.add_argument("port", nargs="?", type=int)
    p.add_argument("--skip-preflight", action="store_true")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("repl", help="run the interactive REPL")
    common(p)
    p.set_defaults(func=cmd_repl)

    p = sub.add_parser("mcp", help="run the MCP adapter")
    common(p)
    p.add_argument(
        "--native", action="store_true",
        help="use the application-owned bounded MCP transport",
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

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except sonder_config.ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
