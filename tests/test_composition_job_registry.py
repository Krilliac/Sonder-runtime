"""Focused composition-root coverage for the durable job registry seam."""
from __future__ import annotations

import pytest

from sonder_runtime.adapters.persistence.sqlite.job_registry import (
    SQLiteDurableJobRegistry,
)
from sonder_runtime.application.capabilities.jobs import JobRegistryService
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
