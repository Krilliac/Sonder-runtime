"""Offline tests for the ``sonder doctor`` stale-resident-model check.

Ollama is supposed to unload a model once its ``keep_alive`` deadline
(``expires_at`` from ``/api/ps``) passes. One still listed well after that
deadline usually means eviction stalled -- a model wedged in VRAM, often left
over from a killed or hung generation. ``_check_ollama_residency`` surfaces
that without ever unloading anything itself (read-only).
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


def test_no_resident_models_reports_ok(monkeypatch):
    _install(monkeypatch, {"models": []})

    result = sonder_doctor._check_ollama_residency()

    assert result["status"] == sonder_doctor.STATUS_OK
    assert "no models resident" in result["detail"]


def test_model_within_keep_alive_reports_ok(monkeypatch):
    _install(
        monkeypatch,
        {"models": [{"name": "sonder:latest", "expires_at": "2999-01-01T00:00:00Z"}]},
    )

    result = sonder_doctor._check_ollama_residency()

    assert result["status"] == sonder_doctor.STATUS_OK
    assert "1 model(s) resident, all within keep_alive" in result["detail"]


def test_model_past_expiry_warns_and_names_it(monkeypatch):
    _install(
        monkeypatch,
        {
            "models": [
                {"name": "sonder:latest", "expires_at": "2001-01-01T00:00:00Z"},
                {"name": "nomic-embed-text", "expires_at": "2999-01-01T00:00:00Z"},
            ]
        },
    )

    result = sonder_doctor._check_ollama_residency()

    assert result["status"] == sonder_doctor.STATUS_WARN
    assert "1/2 resident model(s) past keep_alive expiry" in result["detail"]
    assert "sonder:latest" in result["detail"]
    assert "nomic-embed-text" not in result["detail"]


def test_missing_expiry_field_is_ignored_not_flagged(monkeypatch):
    _install(monkeypatch, {"models": [{"name": "sonder:latest"}]})

    result = sonder_doctor._check_ollama_residency()

    assert result["status"] == sonder_doctor.STATUS_OK


def test_unreachable_ps_endpoint_is_skipped_not_failed(monkeypatch):
    config = types.SimpleNamespace(
        ollama=types.SimpleNamespace(url="http://127.0.0.1:11434")
    )
    monkeypatch.setattr(sonder_doctor, "_load_config_or_none", lambda: config)

    def _boom(*_args, **_kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)

    result = sonder_doctor._check_ollama_residency()

    assert result["status"] == sonder_doctor.STATUS_SKIPPED
    assert "connection refused" in result["detail"]


def test_no_url_configured_is_skipped(monkeypatch):
    config = types.SimpleNamespace(ollama=types.SimpleNamespace(url=""))
    monkeypatch.setattr(sonder_doctor, "_load_config_or_none", lambda: config)

    result = sonder_doctor._check_ollama_residency()

    assert result["status"] == sonder_doctor.STATUS_SKIPPED
