"""Offline tests for the ``sonder doctor`` multi-PC worker check.

``_check_ollama`` only probes the primary Ollama endpoint; a remote worker
outage in a multi-PC pool (``docs/runbooks/multi-pc-ollama.md``) would
otherwise stay invisible until a live request happened to fail over onto it.
``_check_ollama_workers`` closes that gap by probing every configured worker
independently. No network and no config file: the worker list is injected via
a faked ``_load_config_or_none`` and every HTTP response is a stub.
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


def _config(workers):
    return types.SimpleNamespace(
        ollama=types.SimpleNamespace(url="http://127.0.0.1:11434", workers=workers)
    )


def test_skips_when_no_workers_are_configured(monkeypatch):
    monkeypatch.setattr(
        sonder_doctor, "_load_config_or_none", lambda: _config(())
    )

    result = sonder_doctor._check_ollama_workers()

    assert result["status"] == sonder_doctor.STATUS_SKIPPED
    assert "no worker endpoints configured" in result["detail"]


def test_skips_when_config_is_unavailable(monkeypatch):
    monkeypatch.setattr(sonder_doctor, "_load_config_or_none", lambda: None)

    result = sonder_doctor._check_ollama_workers()

    assert result["status"] == sonder_doctor.STATUS_SKIPPED


def test_all_workers_reachable_reports_ok(monkeypatch):
    monkeypatch.setattr(
        sonder_doctor,
        "_load_config_or_none",
        lambda: _config(("https://pc2:443", "https://pc3:443")),
    )
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *a, **k: _Response({"models": [{"name": "sonder:latest"}]}),
    )

    result = sonder_doctor._check_ollama_workers()

    assert result["status"] == sonder_doctor.STATUS_OK
    assert "2 worker(s) reachable" in result["detail"]
    assert "pc2: 1 models" in result["detail"]
    assert "pc3: 1 models" in result["detail"]


def test_one_worker_down_warns_and_names_it(monkeypatch):
    monkeypatch.setattr(
        sonder_doctor,
        "_load_config_or_none",
        lambda: _config(("https://pc2:443", "https://pc3:443")),
    )

    def _urlopen(request, timeout=None):
        if "pc3" in request.full_url:
            raise OSError("connection refused")
        return _Response({"models": []})

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)

    result = sonder_doctor._check_ollama_workers()

    assert result["status"] == sonder_doctor.STATUS_WARN
    assert "1/2 worker(s) unreachable" in result["detail"]
    assert "pc3" in result["detail"]
    assert "connection refused" in result["detail"]
    assert "pc2" in result["detail"]


def test_every_worker_down_fails(monkeypatch):
    monkeypatch.setattr(
        sonder_doctor,
        "_load_config_or_none",
        lambda: _config(("https://pc2:443", "https://pc3:443")),
    )

    def _boom(*_a, **_k):
        raise OSError("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)

    result = sonder_doctor._check_ollama_workers()

    assert result["status"] == sonder_doctor.STATUS_FAIL
    assert "2/2 worker(s) unreachable" in result["detail"]


def test_non_200_worker_response_counts_as_unreachable(monkeypatch):
    monkeypatch.setattr(
        sonder_doctor,
        "_load_config_or_none",
        lambda: _config(("https://pc2:443",)),
    )
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda *a, **k: _Response({}, status=503)
    )

    result = sonder_doctor._check_ollama_workers()

    assert result["status"] == sonder_doctor.STATUS_FAIL
    assert "HTTP 503" in result["detail"]
