"""Focused composition-root coverage for the durable job registry seam."""
from __future__ import annotations

import pytest

from sonder_runtime.adapters.persistence.sqlite.job_registry import (
    SQLiteDurableJobRegistry,
)
from sonder_runtime.application.capabilities.jobs import JobRegistryService
from sonder_runtime.application.jobs.session_lifecycle import JobRegistryLifecycleAdapter
from sonder_runtime.application.ports.jobs import JobIdentity
from sonder_runtime.bootstrap import app as bootstrap_app


pytestmark = pytest.mark.integration


def test_application_exposes_lazy_cached_job_registry(tmp_path, monkeypatch):
    database = tmp_path / "jobs.db"
    monkeypatch.setenv("SONDER_JOBS_DB", str(database))
    bootstrap_app.reset_for_tests()
    try:
        application = bootstrap_app.build_application()

        assert not database.exists()
        first = application.job_registry()
        second = application.job_registry()

        assert isinstance(first, SQLiteDurableJobRegistry)
        assert first is second
        assert database.exists()
    finally:
        bootstrap_app.reset_for_tests()


def test_application_exposes_lazy_cached_job_service(tmp_path, monkeypatch):
    database = tmp_path / "jobs.db"
    monkeypatch.setenv("SONDER_JOBS_DB", str(database))
    bootstrap_app.reset_for_tests()
    try:
        application = bootstrap_app.build_application()

        assert not database.exists()
        first = application.job_service()
        second = application.job_service()

        assert isinstance(first, JobRegistryService)
        assert first is second
        assert isinstance(first._port, SQLiteDurableJobRegistry)
        assert database.exists()
    finally:
        bootstrap_app.reset_for_tests()


def test_application_composes_job_lifecycle_adapter_with_shared_session_store(tmp_path, monkeypatch):
    jobs_database = tmp_path / "jobs.db"
    sessions_database = tmp_path / "sessions.db"
    monkeypatch.setenv("SONDER_JOBS_DB", str(jobs_database))
    monkeypatch.setenv("SONDER_SESSIONS_DB", str(sessions_database))
    bootstrap_app.reset_for_tests()
    try:
        application = bootstrap_app.build_application()
        service = application.job_service()

        assert isinstance(service._lifecycle, JobRegistryLifecycleAdapter)
        assert jobs_database.exists()
        assert sessions_database.exists()

        service.create(JobIdentity("job-linked", "test", "op-1", "idem-1", parent_session_id="session-1"))
        events = application.session_repository().read_range("session-1", limit=10)
        assert [(event.event_type, event.payload["job_id"]) for event in events] == [
            ("job.created", "job-linked"),
        ]
    finally:
        bootstrap_app.reset_for_tests()
