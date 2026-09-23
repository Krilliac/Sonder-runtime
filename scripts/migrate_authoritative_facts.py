"""Plan or explicitly adopt legacy facts into the authoritative fact journal.

This is an operator command.  It never activates automatically and ``apply``
requires an exact plan digest supplied by the same invocation.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sonder_runtime.adapters.memory_store import connect
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
    connection = connect(args.database)
    try:
        plan = plan_legacy_fact_migration(
            connection, source_id=args.source_id, project_scope=args.project,
        )
        summary = {"count": len(plan.rows), "digest": plan.digest, "project": plan.project_scope}
        if not args.apply:
            print(json.dumps(summary, sort_keys=True))
            return 0
        if args.digest != plan.digest:
            parser.error("--apply requires the exact digest from the dry-run plan")
        if args.backup is None:
            parser.error("--apply requires --backup")
        migrated = migrate_legacy_facts(connection, plan, backup_path=args.backup)
        print(json.dumps({**summary, "migrated": migrated}, sort_keys=True))
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
