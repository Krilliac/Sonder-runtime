"""Plan or explicitly adopt legacy facts into the authoritative fact journal.

This is an operator command.  It never activates automatically and ``apply``
requires an exact plan digest supplied by the same invocation.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sonder_runtime.adapters import memory_store
from sonder_runtime.adapters.persistence.owned_sqlite import connect as owned_sqlite_connect
from sonder_runtime.adapters.persistence.sqlite.authoritative_memory import (
    migrate_legacy_facts,
    plan_legacy_fact_migration,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--apply", action="store_true", help="apply the displayed plan")
    parser.add_argument("--digest", help="exact dry-run digest required with --apply")
    parser.add_argument("--backup", type=Path, help="new SQLite backup path required for --apply")
    args = parser.parse_args()
    if (
        not args.database.is_absolute()
        or args.database.is_symlink()
        or not args.database.is_file()
    ):
        parser.error("--database must name an existing absolute regular SQLite file")
    if args.apply and (
        args.backup is None or not args.backup.is_absolute()
    ):
        parser.error("--apply requires an absolute new --backup path")
    # The normal memory_store.connect() initializes and migrates the schema.
    # A dry run must not do that, and apply must back up before any mutation.
    # Fact adoption is defined only for an already current memory schema; an
    # older database needs its own backed-up schema upgrade first.
    database = args.database.resolve()
    if args.apply:
        connection = owned_sqlite_connect(database.as_uri() + "?mode=rw", uri=True)
    else:
        connection = owned_sqlite_connect(database.as_uri() + "?mode=ro", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        if not args.apply:
            connection.execute("PRAGMA query_only=ON")
        if connection.execute("PRAGMA user_version").fetchone()[0] != memory_store._schema_stamp():
            parser.error(
                "database schema is not current; take a backup and upgrade "
                "the schema separately before fact adoption"
            )
        plan = plan_legacy_fact_migration(
            connection, source_id=args.source_id, project_scope=args.project,
        )
        summary = {"count": len(plan.rows), "digest": plan.digest, "project": plan.project_scope}
        if not args.apply:
            print(json.dumps(summary, sort_keys=True))
            return 0
        if args.digest != plan.digest:
            parser.error("--apply requires the exact digest from the dry-run plan")
        migrated = migrate_legacy_facts(connection, plan, backup_path=args.backup)
        print(json.dumps({**summary, "migrated": migrated}, sort_keys=True))
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
