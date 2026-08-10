"""SPEC-5 WP9 — Updates bounded domain tests.

Covers:
- Full update lifecycle (download → verify → backup → drain → activate)
- TUF verification pass/fail
- Activation requires verification AND health check
- Rollback after activation
- Phase gate enforcement
"""
from __future__ import annotations

import pytest

from sonder_runtime.domain.updates.models import (
    ReleaseMetadata,
    UpdatePhase,
    UpdateRun,
)
from sonder_runtime.domain.common.errors import Forbidden, InvalidInput
from sonder_runtime.application.updates.update_service import UpdateService


class InMemoryUpdateStore:
    def __init__(self):
        self._runs: dict[str, UpdateRun] = {}

    def create_run(self, run_id, release):
        run = UpdateRun(
            id=run_id,
            phase=UpdatePhase.DOWNLOADING,
            release=release,
        )
        self._runs[run_id] = run
        return run

    def get_run(self, run_id):
        return self._runs.get(run_id)

    def save_run(self, run):
        self._runs[run.id] = run


class StubVerifier:
    def __init__(self, result=True):
        self._result = result

    def verify(self, release, artifact_path):
        return self._result


RELEASE = ReleaseMetadata(
    version="1.2.0",
    channel="stable",
    digest="sha256:deadbeef",
    size_bytes=1024000,
)


@pytest.fixture
def store():
    return InMemoryUpdateStore()


@pytest.fixture
def svc(store):
    return UpdateService(store, StubVerifier(True))


def _to_draining(svc, store, run_id="u1"):
    """Drive an update to DRAINING phase with health_ok."""
    svc.begin(run_id, RELEASE)
    svc.mark_downloaded(run_id)
    svc.verify(run_id, "/tmp/artifact.tar.gz")
    svc.backup_and_drain(run_id, "/backup/v1.1.0")
    run = store.get_run(run_id)
    run.health_ok = True
    store.save_run(run)
    return run


class TestUpdateLifecycle:
    def test_begin_creates_downloading(self, svc, store):
        run = svc.begin("u1", RELEASE)
        assert run.phase == UpdatePhase.DOWNLOADING
        assert run.release == RELEASE

    def test_mark_downloaded(self, svc, store):
        svc.begin("u1", RELEASE)
        run = svc.mark_downloaded("u1")
        assert run.phase == UpdatePhase.DOWNLOADED

    def test_mark_downloaded_wrong_phase(self, svc, store):
        svc.begin("u1", RELEASE)
        svc.mark_downloaded("u1")
        with pytest.raises(InvalidInput, match="downloading"):
            svc.mark_downloaded("u1")

    def test_verify_pass(self, svc, store):
        svc.begin("u1", RELEASE)
        svc.mark_downloaded("u1")
        run = svc.verify("u1", "/tmp/art.tar.gz")
        assert run.phase == UpdatePhase.VERIFIED
        assert run.verified is True

    def test_verify_fail(self, store):
        svc = UpdateService(store, StubVerifier(False))
        svc.begin("u1", RELEASE)
        svc.mark_downloaded("u1")
        run = svc.verify("u1", "/tmp/art.tar.gz")
        assert run.phase == UpdatePhase.FAILED
        assert run.verified is False

    def test_verify_wrong_phase(self, svc, store):
        svc.begin("u1", RELEASE)
        with pytest.raises(InvalidInput, match="downloaded"):
            svc.verify("u1", "/tmp/art.tar.gz")

    def test_backup_and_drain(self, svc, store):
        svc.begin("u1", RELEASE)
        svc.mark_downloaded("u1")
        svc.verify("u1", "/tmp/a")
        run = svc.backup_and_drain("u1", "/backup/v1")
        assert run.phase == UpdatePhase.DRAINING
        assert run.backup_path == "/backup/v1"

    def test_backup_wrong_phase(self, svc, store):
        svc.begin("u1", RELEASE)
        with pytest.raises(InvalidInput, match="verified"):
            svc.backup_and_drain("u1", "/backup")


class TestActivation:
    def test_activate_with_health(self, svc, store):
        _to_draining(svc, store, "u1")
        run = svc.activate("u1")
        assert run.phase == UpdatePhase.ACTIVATED

    def test_activate_without_health_forbidden(self, svc, store):
        svc.begin("u1", RELEASE)
        svc.mark_downloaded("u1")
        svc.verify("u1", "/tmp/a")
        svc.backup_and_drain("u1", "/backup")
        with pytest.raises(Forbidden, match="health"):
            svc.activate("u1")

    def test_can_activate_property(self, svc, store):
        svc.begin("u1", RELEASE)
        svc.mark_downloaded("u1")
        svc.verify("u1", "/tmp/a")
        svc.backup_and_drain("u1", "/backup")
        run = store.get_run("u1")
        assert run.can_activate is False
        run.health_ok = True
        assert run.can_activate is True


class TestRollback:
    def test_rollback_after_activation(self, svc, store):
        _to_draining(svc, store, "u1")
        svc.activate("u1")
        run = svc.rollback("u1")
        assert run.phase == UpdatePhase.ROLLED_BACK

    def test_rollback_wrong_phase(self, svc, store):
        svc.begin("u1", RELEASE)
        with pytest.raises(InvalidInput, match="activation"):
            svc.rollback("u1")


class TestEdgeCases:
    def test_not_found(self, svc):
        with pytest.raises(InvalidInput, match="not found"):
            svc.mark_downloaded("nonexistent")

    def test_release_immutable(self):
        r = ReleaseMetadata(version="1.0.0")
        with pytest.raises(AttributeError):
            r.version = "2.0.0"

    def test_terminal_phases(self):
        run = UpdateRun(
            id="u1",
            phase=UpdatePhase.ACTIVATED,
            release=RELEASE,
        )
        assert run.is_terminal is True
        run.phase = UpdatePhase.DOWNLOADING
        assert run.is_terminal is False
