"""The local-open warning must reflect the effective, configured auth mode."""

import logging

import sonder_runtime.interfaces.http.serve as ts


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records
            if r.levelno >= logging.WARNING and "local-open" in r.getMessage()]


def test_import_time_resolution_does_not_claim_local_open(caplog):
    """Resolution runs before the secrets file is applied; it must stay quiet."""
    caplog.set_level(logging.DEBUG)
    assert ts._resolve_auth_mode("", False, configured="") == "local-open"
    assert _warnings(caplog) == []


def test_no_local_open_warning_when_api_key_is_configured(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(ts, "API_KEY", "k" * 32)
    monkeypatch.setattr(ts, "AUTH_MODE", "api-key")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)
    assert ts._warn_if_local_open() is False
    assert _warnings(caplog) == []


def test_local_open_warning_when_effectively_unauthenticated(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "local-open")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)
    assert ts._warn_if_local_open() is True
    assert len(_warnings(caplog)) == 1
