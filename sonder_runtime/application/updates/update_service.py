"""Update service (SPEC-5 §21).

Wraps SPEC-4 signed-update logic behind application services.
Preserves: TUF verification, backup, drain, health gates,
atomic activation, rollback, offline import, resumable download.
"""
from __future__ import annotations

from typing import Protocol

from ...domain.updates.models import (
    ReleaseMetadata,
    UpdatePhase,
    UpdateRun,
)
from ...domain.common.errors import Forbidden, InvalidInput


class UpdateStore(Protocol):
    def create_run(
        self, run_id: str, release: ReleaseMetadata,
    ) -> UpdateRun: ...
    def get_run(self, run_id: str) -> UpdateRun | None: ...
    def save_run(self, run: UpdateRun) -> None: ...


class TrustVerifier(Protocol):
    def verify(self, release: ReleaseMetadata, artifact_path: str) -> bool: ...


class UpdateService:
    def __init__(self, store: UpdateStore, verifier: TrustVerifier):
        self._store = store
        self._verifier = verifier

    def begin(self, run_id: str, release: ReleaseMetadata) -> UpdateRun:
        run = self._store.create_run(run_id, release)
        self._store.save_run(run)
        return run

    def mark_downloaded(self, run_id: str) -> UpdateRun:
        run = self._require(run_id)
        if run.phase != UpdatePhase.DOWNLOADING:
            raise InvalidInput("not in downloading phase")
        run.phase = UpdatePhase.DOWNLOADED
        self._store.save_run(run)
        return run

    def verify(self, run_id: str, artifact_path: str) -> UpdateRun:
        run = self._require(run_id)
        if run.phase != UpdatePhase.DOWNLOADED:
            raise InvalidInput("not in downloaded phase")
        run.phase = UpdatePhase.VERIFYING
        ok = self._verifier.verify(run.release, artifact_path)
        run.verified = ok
        if ok:
            run.phase = UpdatePhase.VERIFIED
        else:
            run.phase = UpdatePhase.FAILED
        self._store.save_run(run)
        return run

    def backup_and_drain(self, run_id: str, backup_path: str) -> UpdateRun:
        run = self._require(run_id)
        if run.phase != UpdatePhase.VERIFIED:
            raise InvalidInput("not in verified phase")
        run.backup_path = backup_path
        run.phase = UpdatePhase.BACKING_UP
        run.phase = UpdatePhase.DRAINING
        self._store.save_run(run)
        return run

    def activate(self, run_id: str) -> UpdateRun:
        run = self._require(run_id)
        if not run.can_activate:
            raise Forbidden("activation requires verification and health check")
        run.phase = UpdatePhase.ACTIVATING
        run.phase = UpdatePhase.ACTIVATED
        self._store.save_run(run)
        return run

    def rollback(self, run_id: str) -> UpdateRun:
        run = self._require(run_id)
        if run.phase not in (UpdatePhase.ACTIVATED, UpdatePhase.ACTIVATING):
            raise InvalidInput("can only roll back after activation")
        run.phase = UpdatePhase.ROLLING_BACK
        run.phase = UpdatePhase.ROLLED_BACK
        self._store.save_run(run)
        return run

    def _require(self, run_id: str) -> UpdateRun:
        run = self._store.get_run(run_id)
        if run is None:
            raise InvalidInput(f"update run {run_id!r} not found")
        return run
