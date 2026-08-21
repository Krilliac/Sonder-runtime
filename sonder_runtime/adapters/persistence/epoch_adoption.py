"""Read-only epoch-2 adoption and temporary-schema cleanup checks."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from sonder_runtime.adapters.persistence.migrations import status_read_only
from sonder_runtime.adapters.persistence.sqlite.bridge_migration import (
    EPOCH2_DATABASES,
    check_epoch,
    require_epoch_2,
)


KNOWN_MIGRATION_STORES = MappingProxyType({
    "memory.db": "memory", "operations.db": "operations", "updates.db": "updates",
    "autopilot.db": "autopilot", "fleet.db": "fleet",
})


@dataclass(frozen=True)
class Epoch2CleanupReport:
    allowed: bool
    epoch_by_database: Mapping[str, int | None]
    receipt_valid: bool
    migration_ledgers_healthy: bool
    temporary_schema_objects: Mapping[str, tuple[str, ...]]
    reasons: tuple[str, ...]


def _temporary_objects(path: Path) -> tuple[str, ...]:
    if not path.is_file():
        return ()
    conn = sqlite3.connect(str(path))
    try:
        names = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','index','trigger','view')"
            )
        }
    finally:
        conn.close()
    return tuple(sorted(name for name in names if name.startswith(("tmp_", "bridge_", "legacy_"))))


def check_epoch2_cleanup(
    sonder_home: str | Path,
    *,
    temporary_paths: tuple[str, ...] = (),
) -> Epoch2CleanupReport:
    """Check, without mutating, that epoch 2 is adopted and bridges are clean."""
    home = Path(sonder_home).expanduser().resolve(strict=True)
    reasons: list[str] = []
    epochs = {name: check_epoch(home / name) for name in EPOCH2_DATABASES}
    for name, epoch in epochs.items():
        if epoch != 2:
            reasons.append(f"{name} is not at schema epoch 2")

    receipt_valid = False
    receipt_path = home / "epoch2_adoption_receipt.json"
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt_valid = (
            isinstance(receipt, dict)
            and receipt.get("epoch") == 2
            and isinstance(receipt.get("source_version"), str)
            and bool(receipt["source_version"].strip())
            and isinstance(receipt.get("backup_path"), str)
            and bool(receipt["backup_path"].strip())
        )
    except (OSError, UnicodeError, ValueError, TypeError):
        receipt_valid = False
    if not receipt_valid:
        reasons.append("epoch-2 adoption receipt is missing or invalid")

    ledger_health = True
    for filename, store in KNOWN_MIGRATION_STORES.items():
        path = home / filename
        if not path.is_file():
            continue
        status = status_read_only(store, str(path))
        if status.unknown or status.checksum_mismatches:
            ledger_health = False
            reasons.append(f"{filename} has an unhealthy migration ledger")

    objects = {
        name: found for name in EPOCH2_DATABASES
        if (found := _temporary_objects(home / name))
    }
    if objects:
        reasons.append("temporary schema objects remain")
    leftovers = tuple(path for path in temporary_paths if (home / path).exists())
    if leftovers:
        reasons.append("temporary bridge paths remain: " + ", ".join(leftovers))

    try:
        require_epoch_2(home)
    except Exception as exc:
        reasons.append(f"runtime epoch gate rejected state: {type(exc).__name__}")

    return Epoch2CleanupReport(
        not reasons, MappingProxyType(epochs), receipt_valid, ledger_health,
        MappingProxyType(objects), tuple(reasons),
    )


__all__ = ["EPOCH2_DATABASES", "Epoch2CleanupReport", "check_epoch2_cleanup"]
