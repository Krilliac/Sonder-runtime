"""Automation service (SPEC-5 §11).

Owns all automation run lifecycle: creation, claiming (CAS), progress,
and terminal transitions. Routes agents through RoutePlanner, ModelGateway,
ToolService, and memory services — no independent inference/tool stacks.
"""
from __future__ import annotations

import logging
from typing import Protocol

from ...domain.automation.models import (
    AutomationRun,
    Claim,
    InvalidTransition,
    RunKind,
    RunStatus,
    VALID_TRANSITIONS,
)
from ...domain.common.errors import Forbidden, InvalidInput

logger = logging.getLogger(__name__)


class AutomationStore(Protocol):
    """Persistence port for automation.db."""

    def create_run(
        self,
        run_id: str,
        kind: RunKind,
        objective: str,
        correlation_id: str,
        config: dict | None = None,
    ) -> AutomationRun: ...

    def get_run(self, run_id: str) -> AutomationRun | None: ...

    def list_runs(
        self, kind: RunKind | None = None, include_terminal: bool = False,
    ) -> list[AutomationRun]: ...

    def save_run(self, run: AutomationRun) -> None: ...

    def try_claim(
        self, run_id: str, worker_id: str, lease_seconds: int,
        expected_revision: int,
    ) -> Claim | None: ...

    def release_claim(self, run_id: str, worker_id: str) -> None: ...

    def get_claim(self, run_id: str) -> Claim | None: ...

    def reconcile_expired_claims(self, now_iso: str) -> int: ...


class AutomationService:
    """Owns automation run lifecycle and CAS-based claims."""

    def __init__(self, store: AutomationStore):
        self._store = store

    def create(
        self,
        run_id: str,
        kind: RunKind,
        objective: str,
        correlation_id: str,
        config: dict | None = None,
    ) -> AutomationRun:
        logger.debug(f"AutomationService.create: run_id={run_id!r}, kind={kind.value!r}, correlation_id={correlation_id!r}")
        logger.info(f"automation run creating: run_id={run_id!r}, kind={kind.value!r}")
        if not objective.strip():
            raise InvalidInput("automation run requires an objective")
        return self._store.create_run(
            run_id, kind, objective, correlation_id, config,
        )

    def claim(
        self,
        run_id: str,
        worker_id: str,
        lease_seconds: int = 300,
    ) -> Claim:
        logger.debug(f"AutomationService.claim: run_id={run_id!r}, worker_id={worker_id!r}, lease_seconds={lease_seconds}")
        run = self._store.get_run(run_id)
        if run is None:
            raise InvalidInput(f"run {run_id!r} not found")
        if run.is_terminal:
            raise InvalidInput(f"run {run_id!r} is already terminal")

        logger.info(f"automation run claiming: run_id={run_id!r}, worker_id={worker_id!r}, lease_seconds={lease_seconds}")
        claim = self._store.try_claim(
            run_id, worker_id, lease_seconds, run.revision,
        )
        if claim is None:
            logger.error(f"automation run claim failed: run_id={run_id!r}, worker_id={worker_id!r}, revision={run.revision}")
            logger.warning(f"automation run claim failed (race or revision mismatch): run_id={run_id!r}, worker_id={worker_id!r}, revision={run.revision}")
            raise Forbidden(
                f"run {run_id!r} claim failed — race lost or revision mismatch"
            )

        if run.status == RunStatus.PENDING:
            run.transition(RunStatus.CLAIMED)
            self._store.save_run(run)

        return claim

    def transition(
        self,
        run_id: str,
        worker_id: str,
        new_status: RunStatus,
    ) -> AutomationRun:
        logger.debug(f"AutomationService.transition: run_id={run_id!r}, worker_id={worker_id!r}, new_status={new_status.value!r}")
        run = self._store.get_run(run_id)
        if run is None:
            raise InvalidInput(f"run {run_id!r} not found")

        claim = self._store.get_claim(run_id)
        if claim is not None and claim.worker_id != worker_id:
            raise Forbidden(
                f"run {run_id!r} is claimed by {claim.worker_id!r}"
            )

        run.transition(new_status)
        self._store.save_run(run)
        if new_status == RunStatus.FAILED:
            logger.error(f"automation run failed: run_id={run_id!r}, worker_id={worker_id!r}")
            logger.warning(f"automation run failed: run_id={run_id!r}, worker_id={worker_id!r}")
        logger.info(f"automation run transitioned: run_id={run_id!r}, new_status={new_status.value!r}, is_terminal={run.is_terminal}")

        if run.is_terminal and claim is not None:
            logger.debug(f"AutomationService.transition: run {run_id!r} reached terminal state, releasing claim")
            self._store.release_claim(run_id, worker_id)

        return run

    def reconcile_expired(self, now_iso: str) -> int:
        logger.debug(f"AutomationService.reconcile_expired: now_iso={now_iso!r}")
        count = self._store.reconcile_expired_claims(now_iso)
        if count > 0:
            logger.warning(f"reconciled {count} expired automation claim(s) at {now_iso}")
        logger.debug(f"AutomationService.reconcile_expired: reconciled {count} expired claims")
        return count
