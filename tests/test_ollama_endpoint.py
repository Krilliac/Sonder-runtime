import urllib.request

import pytest

import ollama_endpoint as endpoint


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("127.0.0.1:11434", "http://127.0.0.1:11434"),
        ("http://127.0.0.1:11434/", "http://127.0.0.1:11434"),
        ("[::1]:11434", "http://[::1]:11434"),
        ("0.0.0.0:11434", "http://127.0.0.1:11434"),
        ("[::]:11434", "http://[::1]:11434"),
        ("localhost:11434", "http://127.0.0.1:11434"),
        ("localhost.:11434", "http://127.0.0.1:11434"),
    ],
)
def test_normalize_local_and_bind_all_origins(value, expected):
    assert endpoint.normalize(value) == expected
    assert endpoint.configured_origin(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "http://127.0.0.42:11434",
        "http://[::1]:11434",
        "http://localhost:11434",
    ],
)
def test_loopback_variants_are_private(value):
    assert endpoint.is_loopback(value) is True
    assert endpoint.policy_error(value) == ""


def test_prefix_smuggling_is_not_rewritten_or_trusted(monkeypatch):
    monkeypatch.delenv(endpoint.REMOTE_OPT_IN, raising=False)
    value = "http://0.0.0.0.evil:11434"

    assert endpoint.normalize(value) == value
    assert endpoint.is_loopback(value) is False
    assert "non-loopback" in endpoint.policy_error(value)


@pytest.mark.parametrize(
    "value",
    [
        "file://127.0.0.1:11434",
        "http://user:pass@127.0.0.1:11434",
        "http://127.0.0.1:11434/api",
        "http://127.0.0.1:11434?query=1",
        "http://127.0.0.1:11434#fragment",
        "http://127.0.0.1:not-a-port",
        "http://127.0.0.1",
    ],
)
def test_invalid_endpoint_shapes_fail_closed(value):
    assert endpoint.policy_error(value)
    with pytest.raises(ValueError):
        endpoint.configured_origin(value)


def test_remote_requires_strict_explicit_opt_in(monkeypatch):
    remote = "https://models.example.test:11434"
    monkeypatch.delenv(endpoint.REMOTE_OPT_IN, raising=False)
    assert "non-loopback" in endpoint.policy_error(remote)
    assert endpoint.policy_error(remote, allow_remote="false")

    monkeypatch.setenv(endpoint.REMOTE_OPT_IN, "1")
    assert endpoint.configured_origin(remote) == remote
    assert endpoint.locality(remote) == "remote-opt-in"


def test_locality_distinguishes_invalid_from_blocked_remote(monkeypatch):
    monkeypatch.delenv(endpoint.REMOTE_OPT_IN, raising=False)

    assert endpoint.locality("http://models.example.test:11434") == "remote-blocked"
    assert endpoint.locality("http://127.0.0.1:not-a-port") == "invalid"


def test_client_environment_canonicalizes_without_mutating_bind_environment():
    original = {
        "OLLAMA_HOST": "0.0.0.0:11434",
        "PATH": "keep",
    }

    client = endpoint.client_environment(original)

    assert client["OLLAMA_HOST"] == "http://127.0.0.1:11434"
    assert client["PATH"] == "keep"
    assert original["OLLAMA_HOST"] == "0.0.0.0:11434"


def test_client_environment_uses_passed_remote_consent_not_process_env(monkeypatch):
    monkeypatch.setenv(endpoint.REMOTE_OPT_IN, "1")
    environment = {
        "OLLAMA_HOST": "http://models.example.test:11434",
        endpoint.REMOTE_OPT_IN: "0",
    }

    with pytest.raises(ValueError, match="non-loopback"):
        endpoint.client_environment(environment)


def test_transport_canonicalizes_destination_and_preserves_request(monkeypatch):
    opened = []

    class Opener:
        def open(self, request, timeout=0):
            opened.append((request, timeout))
            return "response"

    monkeypatch.setattr(endpoint, "_OPENER", Opener())
    request = urllib.request.Request(
        "http://0.0.0.0:11434/api/chat?x=1",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    assert endpoint.open_url(request, timeout=7) == "response"
    canonical = opened[0][0]
    assert canonical.full_url == "http://127.0.0.1:11434/api/chat?x=1"
    assert canonical.data == b"{}"
    assert canonical.get_method() == "POST"
    assert canonical.get_header("Content-type") == "application/json"
    assert opened[0][1] == 7


def test_transport_blocks_remote_before_opener(monkeypatch):
    calls = []

    class Opener:
        def open(self, *args, **kwargs):
            calls.append(1)

    monkeypatch.delenv(endpoint.REMOTE_OPT_IN, raising=False)
    monkeypatch.setattr(endpoint, "_OPENER", Opener())

    with pytest.raises(ValueError, match="non-loopback"):
        endpoint.open_url("http://models.example.test:11434/api/chat")
    assert calls == []


def test_transport_disables_proxies_and_redirects():
    redirect = next(
        handler for handler in endpoint._OPENER.handlers
        if isinstance(handler, endpoint._NoRedirect)
    )

    assert endpoint._PROXY_HANDLER.proxies == {}
    assert redirect.redirect_request(None, None, 302, "", {}, "http://other") is None
