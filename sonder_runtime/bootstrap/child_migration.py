"""Offline child aggregate operator commands; no installed cutover authority."""

import argparse
import json

from ..adapters.filesystem.child_migration_bundle import ChildMigrationBundle
from ..adapters.filesystem.file_ops import allowed_roots
from ..adapters.persistence.child_migration import SQLiteChildMigrationStore
from ..application.subagents.child_migration import (
    MigrationRefused,
    MigrationUnsupported,
    export_snapshot,
    stage_snapshot,
    verify_snapshot,
)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "action", choices=("export", "stage", "verify", "status", "resume", "activate")
    )
    result.add_argument("--bundle", required=True)
    result.add_argument("--migration-id")
    for name in ("source", "target"):
        group = result.add_mutually_exclusive_group(required=True)
        group.add_argument("--" + name + "-sqlite")
        group.add_argument("--" + name + "-postgres-binding")
    result.add_argument("--owner-id", default="")
    result.add_argument(
        "--durability", choices=("primary", "sync-pair"), default="primary"
    )
    result.add_argument("--standby", default="")
    return result


def _store(arguments, name):
    path = getattr(arguments, name + "_sqlite")
    if path:
        return SQLiteChildMigrationStore(path)
    from ..platform.child_storage_config import ChildStorageConfig
    from ..adapters.persistence.postgres_binding import PostgresPrivateBinding
    from ..adapters.persistence.postgres_child_migration import (
        PostgresChildMigrationStore,
    )

    binding_path = getattr(arguments, name + "_postgres_binding")
    config = ChildStorageConfig(
        backend="postgresql",
        binding_file=binding_path,
        owner_id=arguments.owner_id,
        durability=arguments.durability,
        required_standby=arguments.standby,
    )
    # This is a fixed private host binding. Credentials and their full closure
    # retain the same live checks as ordinary PostgreSQL application storage.
    binding = PostgresPrivateBinding(binding_path, writable_roots=allowed_roots)
    try:
        return PostgresChildMigrationStore(config, binding)
    except BaseException:
        binding.close()
        raise


def run(arguments):
    if arguments.action == "activate":
        raise MigrationUnsupported(
            "installed service-manager quiescence provider is unavailable"
        )
    if arguments.action != "export" and not arguments.migration_id:
        raise MigrationRefused("same migration identity is required")
    source = target = None
    try:
        with ChildMigrationBundle(
            arguments.bundle, writable_roots=allowed_roots
        ) as bundle:
            if arguments.action == "export":
                source = _store(arguments, "source")
                target = _store(arguments, "target")
                manifest = export_snapshot(
                    source, bundle, target_identity=target.identity
                )
                return {
                    "phase": "SNAPSHOT_SEALED",
                    "migration_id": manifest["migration_id"],
                    "counts": {
                        name: row["count"] for name, row in manifest["streams"].items()
                    },
                    "unresolved": manifest["unresolved"],
                    "active": manifest["active"],
                }
            manifest = bundle.manifest()
            if manifest["migration_id"] != arguments.migration_id:
                raise MigrationRefused("migration identity conflict")
            target = _store(arguments, "target")
            if arguments.action in ("stage", "resume"):
                return stage_snapshot(bundle, target)
            if arguments.action == "verify":
                return verify_snapshot(bundle, target)
            if manifest["target_identity"] != target.identity:
                raise MigrationRefused("migration target identity conflict")
            return target.status(manifest)
    finally:
        for store in (target, source):
            close = getattr(store, "close", None)
            if close is not None and not close(timeout=5):
                raise MigrationRefused(
                    "migration SQL cleanup is incomplete; same-ID reconciliation is required"
                )


def main(argv=None):
    arguments = parser().parse_args(argv)
    try:
        value = run(arguments)
    except MigrationUnsupported:
        value = {
            "status": "unsupported",
            "reason": "installed service-manager quiescence provider is unavailable",
        }
        code = 2
    except Exception:
        # Source payloads, private paths, raw SQL and driver diagnostics must
        # never enter ordinary terminal/transcript output.
        value = {
            "status": "refused",
            "reason": "migration could not be proven; retain the bundle and reconcile the same identity",
        }
        code = 1
    else:
        code = 0
    print(json.dumps(value, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
