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

import sonder_config
import sonder_version


def _load_config(args) -> "sonder_config.SonderConfig":
    overrides = {}
    for item in getattr(args, "set", None) or []:
        if "=" not in item:
            raise sonder_config.ConfigError(
                [f"--set expects section.key=value, got {item!r}"]
            )
        key, _, value = item.partition("=")
        overrides[key.strip()] = value.strip()
    return sonder_config.load_config(
        args.config,
        secrets_path=args.secrets,
        overrides=overrides or None,
    )


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


def cmd_preflight(args) -> int:
    import sonder_preflight

    try:
        config = _load_config(args)
    except sonder_config.ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    report = sonder_preflight.run_preflight(
        config, check_ollama=not args.skip_ollama
    )
    _emit(report.as_dict(), as_json=args.json)
    return 0 if report.ok else 1


def cmd_doctor(args) -> int:
    """Run the consolidated read-only health report."""
    import sonder_doctor

    checks = sonder_doctor.default_checks()
    if args.skip_ollama:
        checks = [(name, check) for name, check in checks if name != "ollama"]
    report = sonder_doctor.run_doctor(checks)
    if args.json:
        _emit(report, as_json=True)
    else:
        print(sonder_doctor.render_report(report))
    return 1 if report.get("overall") == sonder_doctor.STATUS_FAIL else 0


def cmd_status(args) -> int:
    import sonder_migrations

    build = sonder_version.build_info()
    payload: dict = {"build": build.as_dict()}
    try:
        config = _load_config(args)
        payload["profile"] = config.profile
        payload["config_sources"] = list(config.sources)
        _export_runtime_environment(config)
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
    import sonder_migrations
    import sonder_preflight

    payload: dict = {"build": sonder_version.build_info().as_dict()}
    try:
        config = _load_config(args)
        payload["config"] = config.as_redacted_dict()
        payload["preflight"] = sonder_preflight.run_preflight(
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
    import sonder_migrations

    try:
        config = _load_config(args)
        # Migration paths are environment-backed compatibility adapters; without
        # this export --config/--set selected one home but migrated another.
        _export_runtime_environment(config)
        if args.store:
            results = {
                args.store: sonder_migrations.migrate_store(args.store)
            }
        else:
            results = sonder_migrations.migrate_all()
    except sonder_migrations.MigrationError as exc:
        print(f"migration failed: {exc}", file=sys.stderr)
        return 1
    payload = {
        store: {
            "db_path": status.db_path,
            "applied": list(status.applied),
            "pending": list(status.pending),
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
    import sonder_paths

    return str(sonder_paths.default_home() / "backups")


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
    import sonder_backup

    if args.backup_command != "verify":
        config = _load_config(args)
        # Backup source discovery reads SONDER_HOME; exporting only the validated
        # target backed up unrelated state while reporting success.
        _export_runtime_environment(config)
    if args.backup_command == "create":
        result = sonder_backup.create_backup(_backup_target(args, config))
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
            "backup verified", sonder_backup.verify_backup(args.path),
            path=args.path, as_json=args.json,
        )
    if args.backup_command == "list":
        _emit({"backups": sonder_backup.list_backups(_backup_target(args, config))},
              as_json=args.json)
        return 0
    if args.backup_command == "prune":
        if args.keep is not None:
            removed = sonder_backup.prune_backups(
                _backup_target(args, config), keep=args.keep
            )
        else:
            daily = config.backup.retention_daily
            weekly = config.backup.retention_weekly
            monthly = config.backup.retention_monthly
            removed = sonder_backup.prune_backups_tiered(
                _backup_target(args, config), daily=daily, weekly=weekly,
                monthly=monthly,
            )
        _emit({"removed": removed}, as_json=args.json)
        return 0
    raise AssertionError(args.backup_command)


def cmd_restore(args) -> int:
    import sonder_backup

    if args.restore_command == "verify":
        return _report_problems(
            "backup verified", sonder_backup.verify_backup(args.path),
            path=args.path, as_json=args.json,
        )
    if args.restore_command == "smoke":
        return _report_problems(
            "restore smoke passed", sonder_backup.restore_smoke(args.path),
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
        restored = sonder_backup.restore_to_empty(args.path, args.destination)
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
    import sonder_migrations
    import sonder_preflight
    from sonder_operations_store import OperationsStore

    try:
        config = _load_config(args)
    except sonder_config.ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    failures = []
    report = sonder_preflight.run_preflight(config, check_ollama=not args.skip_ollama)
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


def _export_runtime_environment(config) -> None:
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
    os.environ["SONDER_HOST"] = config.server.host
    os.environ["SONDER_PORT"] = str(config.server.port)
    os.environ["SONDER_AUTH_MODE"] = config.server.auth_mode
    os.environ["SONDER_MAX_REQUEST_BYTES"] = str(config.server.max_request_bytes)
    os.environ["OLLAMA_HOST"] = config.ollama.url
    os.environ["SONDER_ALLOW_REMOTE_OLLAMA"] = (
        "1" if config.ollama.allow_remote else "0"
    )
    os.environ["SONDER_WEB_TOOLS"] = "1" if config.features.web else "0"
    os.environ["SONDER_LIVE_RELOAD"] = "1" if config.features.live_reload else "0"
    if config.secrets.api_key:
        os.environ["SONDER_API_KEY"] = config.secrets.api_key
    if config.secrets.auth_secret:
        os.environ["SONDER_AUTH_SECRET"] = config.secrets.auth_secret


def cmd_serve(args) -> int:
    import sonder_preflight

    try:
        config = _load_config(args)
    except sonder_config.ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if not args.skip_preflight:
        report = sonder_preflight.run_preflight(
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
    # The stdlib HTTP adapter still reads its environment at import; feed
    # the validated configuration through the compatibility variables until
    # SPEC-3 gives it a constructor.  This runs before the migration phase:
    # migrations resolve their database paths through SONDER_HOME, so an
    # export after them would migrate the wrong state directory.
    _export_runtime_environment(config)

    # MIGRATING phase: no listener opens until migrations complete.
    import sonder_migrations

    try:
        sonder_migrations.migrate_all(
            busy_timeout_ms=config.state.sqlite_busy_timeout_ms
        )
    except sonder_migrations.MigrationError as exc:
        print(f"migration failed, refusing to bind: {exc}", file=sys.stderr)
        return 1

    import sonder_serve

    sys.argv = ["sonder_serve.py", str(config.server.port)]
    sonder_serve.main()
    return 0


def cmd_repl(args) -> int:
    del args
    import sonder_repl

    sonder_repl.main()
    return 0


def cmd_mcp(args) -> int:
    del args
    import server

    server.run_mcp()
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
    import sonder_update_engine
    import sonder_updates

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
    import sonder_secrets

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
    from sonder_operations_store import OperationsStore

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
    import eval_history

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
    p.add_argument("--json", action="store_true", help="JSON output")
    p.add_argument(
        "--skip-ollama", action="store_true",
        help="do not probe the Ollama endpoint",
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
    p.add_argument("--skip-preflight", action="store_true")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("repl", help="run the interactive REPL")
    p.set_defaults(func=cmd_repl)

    p = sub.add_parser("mcp", help="run the MCP adapter")
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
