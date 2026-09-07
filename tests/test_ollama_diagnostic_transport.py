"""Configured diagnostic probes share Ollama's strict transport, offline."""
from email.message import Message
from io import BytesIO
from types import SimpleNamespace
import urllib.request
from urllib.response import addinfourl

import pytest

import sonder_doctor
from sonder_runtime.adapters import preflight


_REMOTE = "https://worker.example.test:11434"
_PATHS = (
    "preflight_primary", "preflight_workers", "doctor_primary",
    "doctor_workers", "doctor_residency",
)


def _diagnose(monkeypatch, path, origin=_REMOTE, *, allow_remote=True):
    config = SimpleNamespace(ollama=SimpleNamespace(
        url=origin, workers=(origin,), allow_remote=allow_remote,
    ))
    monkeypatch.setattr(sonder_doctor, "_load_config_or_none", lambda: config)
    if path == "preflight_primary":
        return preflight._check_ollama(config, timeout=0.25).ok
    if path == "preflight_workers":
        return preflight._check_ollama_workers(config, timeout=0.25)[0].ok
    check = {
        "doctor_primary": sonder_doctor._check_ollama,
        "doctor_workers": sonder_doctor._check_ollama_workers,
        "doctor_residency": sonder_doctor._check_ollama_residency,
    }[path]
    return check(timeout=0.25)["status"] == sonder_doctor.STATUS_OK


def _fake_wire(monkeypatch, *, redirect=""):
    """Exercise real urllib handlers while replacing only socket transport."""
    for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        monkeypatch.setenv(key, "http://proxy.invalid:3128")
    for key in ("no_proxy", "NO_PROXY"):
        monkeypatch.setenv(key, "")
    # Make the unsafe urlopen path deterministic even on a host with an
    # already-created global opener. Production's private opener stays intact.
    monkeypatch.setattr(urllib.request, "_opener", urllib.request.build_opener(
        urllib.request.ProxyHandler({"https": "http://proxy.invalid:3128"}),
    ))
    calls = []

    def open_connection(_handler, request):
        calls.append((request.host, getattr(request, "_tunnel_host", None),
                      request.full_url, request.timeout))
        headers = Message()
        status = 200
        if redirect and request.full_url.startswith(_REMOTE):
            headers["Location"] = redirect
            status = 302
        response = addinfourl(
            BytesIO(b'{"models": [{"name": "safe-model"}]}'),
            headers, request.full_url, status,
        )
        response.msg = "Found" if status == 302 else "OK"
        return response

    monkeypatch.setattr(urllib.request.HTTPSHandler, "https_open", open_connection)
    monkeypatch.setattr(urllib.request.HTTPHandler, "http_open", open_connection)
    return calls


@pytest.mark.parametrize("path", _PATHS)
def test_configured_remote_diagnostics_ignore_proxy_environment(monkeypatch, path):
    calls = _fake_wire(monkeypatch)

    assert _diagnose(monkeypatch, path)

    endpoint = "/api/ps" if path == "doctor_residency" else "/api/tags"
    assert calls == [("worker.example.test:11434", None, _REMOTE + endpoint, 0.25)]


@pytest.mark.parametrize("path", _PATHS)
@pytest.mark.parametrize("redirect", [
    "https://redirect.example.test:11434/api/tags",
    "http://127.0.0.1:11434/api/tags",
])
def test_configured_remote_diagnostics_never_follow_redirects(monkeypatch, path, redirect):
    calls = _fake_wire(monkeypatch, redirect=redirect)

    assert not _diagnose(monkeypatch, path)

    assert len(calls) == 1
    assert calls[0][:2] == ("worker.example.test:11434", None)
    assert calls[0][2].startswith(_REMOTE)


@pytest.mark.parametrize("path", _PATHS)
@pytest.mark.parametrize("origin,allow_remote", [
    ("http://worker.example.test:11434", True),
    (_REMOTE, False),
    (_REMOTE, "false"),
    (_REMOTE, None),
])
def test_diagnostics_fail_closed_before_network_when_policy_denies(
    monkeypatch, path, origin, allow_remote,
):
    calls = _fake_wire(monkeypatch)
    monkeypatch.setenv("SONDER_ALLOW_REMOTE_OLLAMA", "1")

    assert not _diagnose(monkeypatch, path, origin, allow_remote=allow_remote)

    assert calls == []


@pytest.mark.parametrize("path", _PATHS)
def test_loopback_diagnostics_remain_available_without_remote_consent(monkeypatch, path):
    calls = _fake_wire(monkeypatch)

    assert _diagnose(monkeypatch, path, "http://127.0.0.1:11434", allow_remote=False)

    assert len(calls) == 1
    assert calls[0][:2] == ("127.0.0.1:11434", None)
