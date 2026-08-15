"""Offline tests for the ``sonder doctor`` Ollama catalog check.

No network and no config file: the endpoint URL is injected by faking
``_load_config_or_none`` and the HTTP response is a stub context manager.

The contract under test: reachability alone is not readiness. An Ollama that
answers ``/api/tags`` with an empty catalog has no model to serve, so reporting
plain ``ok`` there tells an operator the opposite of what the next chat turn
will do (see ``setup_alias.py``, which is what creates the ``sonder:latest``
alias the local route resolves).
"""
import json
import types
import urllib.request

import sonder_doctor


class _Response:
    def __init__(self, payload, status=200):
        self._body = json.dumps(payload).encode("utf-8")
        self.status = status

    def read(self, _limit=None):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _install(monkeypatch, payload, *, status=200, url="http://127.0.0.1:11434"):
    config = types.SimpleNamespace(ollama=types.SimpleNamespace(url=url))
    monkeypatch.setattr(sonder_doctor, "_load_config_or_none", lambda: config)
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda *a, **k: _Response(payload, status)
    )


def test_empty_catalog_warns_instead_of_reporting_ok(monkeypatch):
    _install(monkeypatch, {"models": []})

    result = sonder_doctor._check_ollama()

    assert result["status"] == sonder_doctor.STATUS_WARN
    assert "127.0.0.1" in result["detail"]
    assert "no models installed" in result["detail"]
    assert "setup_alias.py" in result["detail"]


def test_absent_models_key_is_treated_as_an_empty_catalog(monkeypatch):
    _install(monkeypatch, {})

    result = sonder_doctor._check_ollama()

    assert result["status"] == sonder_doctor.STATUS_WARN
    assert "no models installed" in result["detail"]


def test_populated_catalog_still_reports_ok_with_the_count(monkeypatch):
    _install(
        monkeypatch,
        {"models": [{"name": "sonder:latest"}, {"name": "nomic-embed-text"}]},
    )

    result = sonder_doctor._check_ollama()

    assert result["status"] == sonder_doctor.STATUS_OK
    assert result["detail"] == "127.0.0.1: 2 models"


def test_non_200_response_still_fails(monkeypatch):
    _install(monkeypatch, {"models": []}, status=503)

    result = sonder_doctor._check_ollama()

    assert result["status"] == sonder_doctor.STATUS_FAIL
    assert "503" in result["detail"]


def test_unreachable_endpoint_still_fails(monkeypatch):
    config = types.SimpleNamespace(
        ollama=types.SimpleNamespace(url="http://127.0.0.1:11434")
    )
    monkeypatch.setattr(sonder_doctor, "_load_config_or_none", lambda: config)

    def _boom(*_args, **_kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)

    result = sonder_doctor._check_ollama()

    assert result["status"] == sonder_doctor.STATUS_FAIL
    assert "connection refused" in result["detail"]


def test_empty_catalog_rolls_up_to_warn_not_fail(monkeypatch):
    """A warn keeps ``sonder doctor`` exit code 0; only fail exits 1."""
    _install(monkeypatch, {"models": []})

    report = sonder_doctor.run_doctor(
        {"ollama": sonder_doctor._check_ollama, "config": lambda: {
            "status": sonder_doctor.STATUS_OK, "detail": "loaded",
        }}
    )

    assert report["overall"] == sonder_doctor.STATUS_WARN
