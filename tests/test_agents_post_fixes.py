"""Post-lane fixes for the agents lane: cancel polling, nested fences, watcher resilience, bridge close."""
from __future__ import annotations

import logging
import threading
import time

import pytest

from sonder_runtime.adapters.execution import effect_fence
from sonder_runtime.adapters.inference import sonder_inference_gateway as gateway_module
from sonder_runtime.application.agents import delegation_runtime as rt
from sonder_runtime.bootstrap import delegation_wiring

pytestmark = pytest.mark.unit


def test_composite_with_event_notices_the_event_promptly():
    """An Event inside a composite with a slow check token is noticed at the Event's rate."""
    event = threading.Event()
    composite = gateway_module._CompositeCancellation(lambda: False, event)
    with gateway_module._ExchangeBudget(30.0, cancellation=composite) as budget:
        started = time.monotonic()
        event.set()
        while not budget.aborted and time.monotonic() - started < 2.0:
            time.sleep(0.005)
        assert budget.aborted and time.monotonic() - started < 0.4


def test_composite_does_not_poll_its_slow_check_at_the_fast_rate():
    calls = []

    def check():
        calls.append(time.monotonic())
        return False

    composite = gateway_module._CompositeCancellation(check, threading.Event())
    with gateway_module._ExchangeBudget(30.0, cancellation=composite):
        time.sleep(1.2)
    assert 1 <= len(calls) <= 5, "slow check polled %d times in 1.2 s" % len(calls)


def test_fleet_fence_under_a_nested_non_fleet_fence_is_found():
    fleet = effect_fence.Fence("fleet:a1", lambda: "", fleet_agent_id="a1")
    inner = effect_fence.Fence("autopilot:x", lambda: "")
    with effect_fence.held(fleet):
        assert effect_fence.current_fleet_agent_id() == "a1"
        with effect_fence.held(inner):
            assert effect_fence.current() is inner
            assert effect_fence.current_fleet_agent_id() == "a1"
        assert effect_fence.current_fleet_agent_id() == "a1"
    assert effect_fence.current_fleet_agent_id() == ""


def test_innermost_fleet_fence_wins_over_an_outer_one():
    outer = effect_fence.Fence("fleet:o", lambda: "", fleet_agent_id="outer")
    inner = effect_fence.Fence("fleet:i", lambda: "", fleet_agent_id="inner")
    with effect_fence.held(outer), effect_fence.held(inner):
        assert effect_fence.current_fleet_agent_id() == "inner"


def test_watcher_survives_a_raising_cancel_check_and_logs_once(caplog):
    state = {"calls": 0, "cancel": False}

    def check():
        state["calls"] += 1
        if state["cancel"]:
            return True
        raise RuntimeError("database is locked")

    original = gateway_module._WATCH_RETRY_SECONDS
    gateway_module._WATCH_RETRY_SECONDS = (0.01, 0.02)
    try:
        with caplog.at_level(logging.WARNING, logger=gateway_module.logger.name):
            with gateway_module._ExchangeBudget(30.0, cancellation=check) as budget:
                deadline = time.monotonic() + 3.0
                while state["calls"] < 4 and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert state["calls"] >= 4, "the watcher died after the first failure"
                assert not budget.aborted
                state["cancel"] = True
                deadline = time.monotonic() + 3.0
                while not budget.aborted and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert budget.aborted, "the watcher did not recover and see the cancel"
    finally:
        gateway_module._WATCH_RETRY_SECONDS = original
    locked = [r for r in caplog.records if "database is locked" in r.getMessage()]
    assert len(locked) == 1


def test_bridge_close_closes_the_stack_when_the_terminal_event_raises(monkeypatch):
    closed = []

    class _Stack:
        def close(self):
            closed.append(True)

    monkeypatch.setattr(rt, "emit_agent_event", lambda **kwargs: None)
    bridge = delegation_wiring.AgentImplBridge(
        stack=_Stack(), prompt="p", model="m", max_steps=1, cloud=False, read_only=True, project_scope="",
    )

    def boom(**kwargs):
        raise RuntimeError("sink failed")

    monkeypatch.setattr(rt, "emit_agent_event", boom)
    with pytest.raises(RuntimeError):
        bridge.close(ok=True)
    assert closed == [True]


def test_cancel_poll_helper_has_one_source_of_truth():
    assert not hasattr(gateway_module, "_cancel_poll_seconds")


def test_bridge_init_closes_the_stack_when_the_starting_event_raises(monkeypatch):
    closed = []

    class _Stack:
        def close(self):
            closed.append(True)

    def boom(**kwargs):
        raise RuntimeError("sink failed")

    monkeypatch.setattr(rt, "emit_agent_event", boom)
    with pytest.raises(RuntimeError):
        delegation_wiring.AgentImplBridge(
            stack=_Stack(), prompt="p", model="m", max_steps=1, cloud=False, read_only=True, project_scope="",
        )
    assert closed == [True]


_LOCAL_ENV = {
    "SONDER_INFERENCE_BASE_URL": "http://127.0.0.1:8765",
    "SONDER_INFERENCE_FALLBACK": "",
    "SONDER_OLLAMA_WORKERS": "",
    "SONDER_ALLOW_REMOTE_OLLAMA": "",
    "SONDER_ALLOW_REMOTE_INFERENCE": "",
    "SONDER_INFERENCE_PRIVATE_WORKERS": "",
}


def _env(monkeypatch, **overrides):
    for name, value in {**_LOCAL_ENV, **overrides}.items():
        monkeypatch.setenv(name, value)


def test_sonder_inference_on_loopback_without_remote_routes_is_local(monkeypatch):
    from sonder_runtime.adapters import restricted_tool_lane as lane

    _env(monkeypatch)
    assert lane._is_non_local_provider("sonder_inference") is False
    _env(monkeypatch, SONDER_INFERENCE_FALLBACK="ollama")
    assert lane._is_non_local_provider("sonder_inference") is False


def test_sonder_inference_with_ollama_fallback_and_remote_workers_is_non_local(monkeypatch):
    from sonder_runtime.adapters import restricted_tool_lane as lane

    _env(
        monkeypatch, SONDER_INFERENCE_FALLBACK="ollama",
        SONDER_OLLAMA_WORKERS="https://10.0.0.5:11434", SONDER_ALLOW_REMOTE_OLLAMA="1",
    )
    assert lane._is_non_local_provider("sonder_inference") is True


def test_sonder_inference_with_private_workers_is_non_local(monkeypatch, tmp_path):
    import json

    from sonder_runtime.adapters import restricted_tool_lane as lane

    ca = tmp_path / "ca.pem"
    ca.write_text("x")
    workers = json.dumps([{"url": "https://10.0.0.7:8443", "ca_bundle": str(ca), "token_env": "WORKER_TOKEN_X"}])
    _env(monkeypatch, SONDER_ALLOW_REMOTE_INFERENCE="1", SONDER_INFERENCE_PRIVATE_WORKERS=workers)
    assert lane._is_non_local_provider("sonder_inference") is True


def test_sonder_inference_with_unreadable_fallback_setting_is_non_local(monkeypatch):
    from sonder_runtime.adapters import restricted_tool_lane as lane

    _env(monkeypatch, SONDER_INFERENCE_FALLBACK="bogus")
    assert lane._is_non_local_provider("sonder_inference") is True
