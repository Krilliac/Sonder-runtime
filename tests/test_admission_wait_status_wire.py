"""The GPU-admission wait status on the wire, and the start-up prewarm wiring in serve."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

import server
import sonder_runtime.interfaces.http.serve as ts
from sonder_runtime.platform import config as sonder_config
from tests.support.gpu_admission_harness import (
    DIGEST, GIB, MODEL, FakeOllama, chat_body, make_service, wire,
)
from tests.test_serve_sse_keepalive import _http_server, _stream_request


def _low_ram_service(tmp_path, monkeypatch, state):
    fake = FakeOllama()
    service = make_service(tmp_path, fake, host=(12 * GIB, 10 * GIB), on_host_floor="queue",
                           host_recheck_interval_ms=100)
    domain_reading = type(service._host_reader())
    service._host_reader = lambda: domain_reading(
        available_physical_bytes=state["available"], total_physical_bytes=64 * GIB,
        commit_limit_bytes=96 * GIB, commit_available_bytes=64 * GIB, commit_enforced=True)
    wire(monkeypatch, server, fake, service)
    service.record_footprint(DIGEST, 16384, 17 * GIB, 17 * GIB)
    return fake, service


def test_streamed_turn_with_a_queued_admission_sends_status_then_clear(tmp_path, monkeypatch):
    state = {"available": 12 * GIB}
    fake, _service = _low_ram_service(tmp_path, monkeypatch, state)

    def answer(*_args, **_kwargs):
        server._post_model("/api/chat", chat_body(16384), model=MODEL, timeout=10)
        return "late answer"

    def release_once_waiting(received):
        # Free the memory only once the client has seen the wait. A fixed
        # timer raced the request: on Windows the stream's first byte alone
        # can take seconds, so the RAM came back before admission queued.
        if b": sonder-status waiting" in received:
            state.update(available=40 * GIB)

    monkeypatch.setattr(ts._legacy_runtime(), "answer_with_history", answer)
    with _http_server(monkeypatch) as port:
        text = _stream_request(port, on_received=release_once_waiting)

    _head, _, stream = text.partition("\r\n\r\n")
    lines = stream.splitlines()
    statuses = [i for i, line in enumerate(lines) if line.startswith(": sonder-status")]
    assert statuses, "a queued load must put a status comment on the wire"
    first = lines[statuses[0]]
    assert first.startswith(": sonder-status waiting for memory to load ")
    assert "12.0 GiB free" in first
    assert lines[statuses[-1]] == ": sonder-status", "admission must end with the clearing frame"
    answer_at = next(i for i, line in enumerate(lines) if line.startswith("data: ") and "late answer" in line)
    assert statuses[-1] < answer_at
    assert len(fake.chat_bodies) == 1


def test_no_clear_frame_when_no_status_was_sent(tmp_path, monkeypatch):
    state = {"available": 64 * GIB}
    _low_ram_service(tmp_path, monkeypatch, state)

    def answer(*_args, **_kwargs):
        server._post_model("/api/chat", chat_body(16384), model=MODEL, timeout=10)
        return "prompt answer"

    monkeypatch.setattr(ts._legacy_runtime(), "answer_with_history", answer)
    with _http_server(monkeypatch) as port:
        text = _stream_request(port)
    assert ": sonder-status" not in text


# -- serve.main start-up hook and the typed-config mapping ---------------------

def test_configure_typed_config_maps_prewarm_on_start(monkeypatch):
    for name in ("_ARTIFACT_TRANSFER_BINDING", "_ARTIFACT_TRANSFER_CONFIG", "CONFIGURED_PORT",
                 "API_KEY", "AUTH_SECRET", "HOST", "REQUIRE_ACCOUNT", "AUTH_MODE", "CORS_ORIGINS",
                 "TLS_TERMINATED_BY_PROXY", "ALLOW_REGISTRATION", "MAX_REQUEST_BYTES",
                 "MAX_DISCARDED_BODY_BYTES", "REQUEST_TIMEOUT_SECONDS", "STREAM_IDLE_TIMEOUT_SECONDS",
                 "STREAM_HEARTBEAT_SECONDS", "PREWARM_ON_START",
                 "HTTP_SESSION_STATE_LIMIT", "HTTP_SESSION_STATE_OWNER_LIMIT", "TRAIN_MAX_N",
                 "_HEALTH_STATUS_FACADE", "_TRUSTED_PROXY_NETWORKS"):
        monkeypatch.setattr(ts, name, getattr(ts, name))
    base = sonder_config.SonderConfig(
        secrets=sonder_config.Secrets(api_key="administrator-" + "a" * 32))
    off = replace(base, server=replace(base.server, prewarm_on_start=False))
    ts.configure_typed_config(off)
    assert ts.PREWARM_ON_START is False
    on = replace(base, server=replace(base.server, prewarm_on_start=True))
    ts.configure_typed_config(on)
    assert ts.PREWARM_ON_START is True


@pytest.mark.parametrize("raw, expected", [
    ("1", True), ("true", True), ("yes", True), ("on", True),
    ("0", False), ("false", False), ("off", False), ("garbage", False),
])
def test_serve_fallback_env_parse_agrees_with_the_typed_config(raw, expected):
    from sonder_runtime.platform.config_environment import env_bool

    assert env_bool(raw) is expected
    typed = sonder_config.load_config(env={"SONDER_PREWARM_ON_START": raw})
    assert typed.server.prewarm_on_start is expected


def _run_serve_main(monkeypatch, setting, recorded):
    """Run serve.main to the listener with every heavy collaborator replaced."""
    lifecycle = SimpleNamespace(
        startup=lambda **_kwargs: None,
        begin_ollama_probe=lambda: None,
        stop_probe=lambda: None,
        coordinator=SimpleNamespace(add_flush_hook=lambda _hook: None, draining=False),
        drain=lambda _reason: True,
    )

    class _Listener:
        def __init__(self, *_args):
            pass

        def serve_forever(self):
            return None

        def server_close(self):
            return None

    for name in ("_ARTIFACT_TRANSFER_BINDING", "_APP_CONTROL_BINDING",
                 "_MEMORY_REPLICATION_RECEIVER", "_MEMORY_REPLICATION_SERVICE"):
        monkeypatch.setattr(ts, name, None)
    monkeypatch.setattr(ts, "_SESSION_FACADE", object())
    monkeypatch.setattr(ts, "_CONTROL_PLANE_SERVICE", object())
    monkeypatch.setattr(ts, "PREWARM_ON_START", setting)
    monkeypatch.setattr(ts.sonder_lifecycle, "get", lambda: lifecycle)
    monkeypatch.setattr(ts, "_validate_bind_security", lambda _host: None)
    monkeypatch.setattr(ts, "_machine_host_names", lambda: None)
    monkeypatch.setattr(ts, "_selected_listener_port", lambda _config: 0)
    monkeypatch.setattr(ts, "_WORK_RUNNER", SimpleNamespace(reconcile=lambda: None))
    monkeypatch.setattr(ts, "_register_telemetry_drain", lambda *_a, **_k: False)
    monkeypatch.setattr(ts, "_live_telemetry_application", lambda: None)
    monkeypatch.setattr(ts, "_warn_if_local_open", lambda: None)
    monkeypatch.setattr(ts.command_catalog, "http_slash_tools", lambda: None)
    monkeypatch.setattr(ts.command_catalog, "catalog", lambda: None)
    monkeypatch.setattr(ts._legacy_runtime(), "runtime_source_update_status",
                        lambda refresh=False: "status")
    monkeypatch.setattr(ts.startup_prewarm, "start_default_model_prewarm",
                        lambda **kwargs: recorded.append(kwargs) or True)
    ts.main(None, _server_factory=_Listener, _close_default_resources=False)


def test_serve_main_starts_the_default_model_prewarm_after_the_listener_is_bound(monkeypatch):
    recorded = []
    _run_serve_main(monkeypatch, True, recorded)
    assert len(recorded) == 1
    call = recorded[0]
    assert call["setting"] is True
    assert call["thread_factory"] is ts.owned_runtime_thread
    seen = []
    monkeypatch.setattr(ts._legacy_runtime(), "prewarm_model",
                        lambda tier="", **kwargs: seen.append((tier, kwargs)) or True)
    assert call["prewarm"]("") is True
    assert seen == [("", {"announce": True})]  # the start-up load is announced at INFO
    monkeypatch.setattr(ts._legacy_runtime().sonder_speculation, "prewarm_enabled", lambda: True)
    assert call["enabled"]() is True


def test_serve_main_passes_the_off_setting_to_the_hook(monkeypatch):
    recorded = []
    _run_serve_main(monkeypatch, False, recorded)
    assert [call["setting"] for call in recorded] == [False]
