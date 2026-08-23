from __future__ import annotations

from urllib.error import URLError

import pytest

from sonder_runtime.adapters.inference.ollama_pool import (
    OllamaWorkerPool,
    from_environment,
    parse_worker_origins,
    validate_worker_origin,
)


def test_worker_origin_parser_accepts_comma_and_semicolon_lists():
    assert parse_worker_origins("https://a:11434; https://b:11434, https://a:11434") == (
        "https://a:11434", "https://b:11434", "https://a:11434",
    )


def test_remote_worker_requires_https_and_explicit_consent():
    with pytest.raises(ValueError, match="require SONDER_ALLOW_REMOTE_OLLAMA"):
        validate_worker_origin("http://192.168.1.20:11434", allow_remote=False)
    with pytest.raises(ValueError, match="must use https"):
        validate_worker_origin("http://192.168.1.20:11434", allow_remote=True)
    assert validate_worker_origin(
        "https://192.168.1.20:11434/", allow_remote=True
    ) == "https://192.168.1.20:11434"


def test_pool_fails_over_only_on_transport_failure():
    pool = OllamaWorkerPool(
        "http://127.0.0.1:11434",
        ("https://worker-a.example:11434", "https://worker-b.example:11434"),
        allow_remote=True,
    )
    calls = []

    def send(origin):
        calls.append(origin)
        if origin == "http://127.0.0.1:11434":
            raise URLError("primary unavailable")
        return {"worker": origin}

    result = pool.request(send)

    assert result["worker"] != "http://127.0.0.1:11434"
    assert calls[0] == "http://127.0.0.1:11434"
    assert len(calls) == 2
    assert all(snapshot.consecutive_failures == 0 for snapshot in pool.snapshots() if snapshot.origin != calls[0])


def test_pool_does_not_fail_over_after_a_non_transport_failure():
    pool = OllamaWorkerPool(
        "http://127.0.0.1:11434", ("http://127.0.0.2:11434",)
    )
    calls = []

    def send(origin):
        calls.append(origin)
        raise ValueError("invalid request")

    with pytest.raises(ValueError, match="invalid request"):
        pool.request(send)
    assert len(calls) == 1


def test_pool_circuit_breaker_cools_a_repeatedly_failed_worker():
    pool = OllamaWorkerPool(
        "http://127.0.0.1:11434",
        ("http://127.0.0.2:11434",),
        failure_threshold=1,
        cooldown_seconds=60,
    )
    calls = []

    def send(origin):
        calls.append(origin)
        if origin == "http://127.0.0.1:11434":
            raise URLError("primary unavailable")
        return origin

    pool.request(send)
    pool.request(send)
    status = pool.status()
    primary = next(item for item in status["workers"] if item["origin"].endswith(".1:11434"))
    assert primary["healthy"] is False
    assert primary["consecutive_failures"] == 1
    assert calls.count("http://127.0.0.1:11434") == 1


def test_environment_builder_keeps_single_worker_compatible():
    pool = from_environment(
        "http://127.0.0.1:11434",
        {"SONDER_OLLAMA_WORKERS": "", "SONDER_ALLOW_REMOTE_OLLAMA": "0"},
    )
    assert pool.enabled is False
    assert pool.origins == ("http://127.0.0.1:11434",)


def test_server_posts_through_the_pool_selected_origin(monkeypatch):
    import server

    class FakePool:
        enabled = True
        origins = ("http://127.0.0.1:11434", "https://worker.example:11434")

        def request(self, sender):
            return sender("https://worker.example:11434")

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return b'{"ok": true}'

    seen = []

    def open_url(request, timeout=0):
        seen.append((request.full_url, timeout))
        return Response()

    monkeypatch.setattr(server, "OLLAMA_POOL", FakePool())
    monkeypatch.setattr(server.ollama_endpoint, "open_url", open_url)

    assert server._post("/api/tags", {}) == {"ok": True}
    assert seen[0][0] == "https://worker.example:11434/api/tags"
