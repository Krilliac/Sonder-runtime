"""Fail-closed, idempotent execution of typed reconciliation decisions."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .startup_reconciliation import (
    DrainAction,
    ReconciliationClass,
    ReconciliationResult,
)


class RepairConflict(RuntimeError):
    """A repair key was reused for a different reconciliation decision."""


class RepairRecoveryRequired(RuntimeError):
    """A prior effect may have run, so automatic replay is unsafe."""


class RepairJournalPort(Protocol):
    def get(self, repair_id: str) -> Mapping[str, Any] | None: ...
    def put_if_absent(self, repair_id: str, record: dict[str, Any]) -> tuple[Mapping[str, Any], bool]: ...
    def replace(self, repair_id: str, record: dict[str, Any]) -> None: ...


@dataclass(frozen=True, slots=True)
class RepairExecution:
    repair_id: str
    record_id: str
    action: DrainAction
    status: str
    value: Any = None
    replayed: bool = False


_SAFE_ACTIONS = frozenset({DrainAction.RESUME, DrainAction.DELIVER, DrainAction.MARK_INTERRUPTED})


def _fingerprint(result: ReconciliationResult) -> str:
    item = result.observation
    payload = {
        "record_id": item.record_id,
        "kind": item.kind.value,
        "status": item.status,
        "classification": result.classification.value,
        "action": result.action.value,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class DurableRepairExecutor:
    """Execute only local typed recovery effects, once per durable key.

    A pending journal entry is fail-closed: after an interrupted process the
    caller must reconcile the external effect before deciding what to do.
    """

    def __init__(self, journal: RepairJournalPort) -> None:
        self._journal = journal

    def execute(
        self,
        repair_id: str,
        result: ReconciliationResult,
        effect: Callable[[ReconciliationResult], Any],
    ) -> RepairExecution:
        if not isinstance(repair_id, str) or not repair_id.strip():
            raise ValueError("repair_id is required")
        if not callable(effect):
            raise TypeError("effect must be callable")
        self._validate(result)
        fingerprint = _fingerprint(result)
        existing = self._journal.get(repair_id)
        if existing is not None:
            if existing.get("fingerprint") != fingerprint:
                raise RepairConflict("repair_id is bound to another decision")
            if existing.get("status") == "pending":
                raise RepairRecoveryRequired("repair effect requires external reconciliation")
            return RepairExecution(repair_id, result.observation.record_id, result.action, existing["status"], existing.get("value"), True)

        pending = {"record_id": result.observation.record_id, "action": result.action.value, "fingerprint": fingerprint, "status": "pending"}
        winner, created = self._journal.put_if_absent(repair_id, pending)
        if winner.get("fingerprint") != fingerprint:
            raise RepairConflict("repair_id is bound to another decision")
        if not created:
            if winner.get("status") == "pending":
                raise RepairRecoveryRequired("repair effect requires external reconciliation")
            return RepairExecution(repair_id, result.observation.record_id, result.action, winner["status"], winner.get("value"), True)

        value = effect(result)
        completed = {**pending, "status": "applied", "value": value}
        self._journal.replace(repair_id, completed)
        return RepairExecution(repair_id, result.observation.record_id, result.action, "applied", value)

    @staticmethod
    def _validate(result: ReconciliationResult) -> None:
        if result.action not in _SAFE_ACTIONS:
            raise RepairConflict("reconciliation action is not locally executable")
        if result.action in {DrainAction.RESUME, DrainAction.DELIVER} and result.classification is not ReconciliationClass.RESUMABLE:
            raise RepairConflict("resume or delivery requires resumable classification")
        if result.action is DrainAction.MARK_INTERRUPTED and result.classification not in {ReconciliationClass.INTERRUPTED, ReconciliationClass.ORPHANED}:
            raise RepairConflict("mark-interrupted requires interrupted or orphaned classification")


__all__ = ["DurableRepairExecutor", "RepairConflict", "RepairExecution", "RepairJournalPort", "RepairRecoveryRequired"]
