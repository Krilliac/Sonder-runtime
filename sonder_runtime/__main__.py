"""``python -m sonder_runtime`` — production command surface (SPEC-2 WP1).

Commands:

    serve         run the HTTP adapter (preflight first, fail closed)
    mcp           run the MCP adapter
    repl          run the interactive REPL
    preflight     run startup checks and report without binding
    status        local runtime/build/schema status
    diagnostics   privacy-safe diagnostic bundle (redacted)
    config        show the effective redacted configuration
    migrate       apply pending schema migrations
    backup        create / verify / list / prune backups
    restore       verify / apply a backup into an empty directory
    drain         request graceful drain of a running server
    smoke         minimal end-to-end check without a real model
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


def cmd_status(args) -> int:
    import sonder_migrations

    build = sonder_version.build_info()
    payload: dict = {"build": build.as_dict()}
    try:
        config = _load_config(args)
        payload["profile"] = config.profile
        payload["config_sources"] = list(config.sources)
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


def _backup_target(args) -> str:
    if args.target:
        return args.target
    try:
        config = _load_config(args)
    except sonder_config.ConfigError:
        config = None
    if config and config.backup.target:
        return config.backup.target
    import sonder_paths

    return str(sonder_paths.default_home() / "backups")


def cmd_backup(args) -> int:
    import sonder_backup

    if args.backup_command == "create":
        result = sonder_backup.create_backup(_backup_target(args))
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
        problems = sonder_backup.verify_backup(args.path)
        if problems:
            for problem in problems:
                print(f"FAIL: {problem}", file=sys.stderr)
            return 1
        print("backup verified")
        return 0
    if args.backup_command == "list":
        _emit({"backups": sonder_backup.list_backups(_backup_target(args))},
              as_json=args.json)
        return 0
    if args.backup_command == "prune":
        removed = sonder_backup.prune_backups(
            _backup_target(args), keep=args.keep
        )
        _emit({"removed": removed}, as_json=args.json)
        return 0
    raise AssertionError(args.backup_command)


def cmd_restore(args) -> int:
    import sonder_backup

    if args.restore_command == "verify":
        problems = sonder_backup.verify_backup(args.path)
        if problems:
            for problem in problems:
                print(f"FAIL: {problem}", file=sys.stderr)
            return 1
        print("backup verified")
        return 0
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
    # MIGRATING phase: no listener opens until migrations complete.
    import sonder_migrations

    try:
        sonder_migrations.migrate_all(
            busy_timeout_ms=config.state.sqlite_busy_timeout_ms
        )
    except sonder_migrations.MigrationError as exc:
        print(f"migration failed, refusing to bind: {exc}", file=sys.stderr)
        return 1
    # The stdlib HTTP adapter still reads its environment at import; feed
    # the validated configuration through the compatibility variables until
    # SPEC-3 gives it a constructor.
    os.environ["SONDER_HOST"] = config.server.host
    os.environ["SONDER_PORT"] = str(config.server.port)
    os.environ["SONDER_AUTH_MODE"] = config.server.auth_mode
    os.environ["SONDER_MAX_REQUEST_BYTES"] = str(config.server.max_request_bytes)
    if config.secrets.api_key:
        os.environ["SONDER_API_KEY"] = config.secrets.api_key
    if config.secrets.auth_secret:
        os.environ["SONDER_AUTH_SECRET"] = config.secrets.auth_secret

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

    server.mcp.run()
    return 0


def cmd_drain(args) -> int:
    del args
    print(
        "drain: the HTTP admin drain endpoint arrives with SPEC-2 WP3; "
        "until then stop the service manager unit (systemctl stop sonder) "
        "which delivers SIGTERM and triggers the in-process drain.",
        file=sys.stderr,
    )
    return 2


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
            bp.add_argument("--keep", type=int, default=7)
    bp = backup_sub.add_parser("verify")
    bp.add_argument("path")
    bp.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("restore", help="restore management")
    restore_sub = p.add_subparsers(dest="restore_command", required=True)
    rp = restore_sub.add_parser("verify")
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
