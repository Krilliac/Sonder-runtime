"""Start-up prewarm of the default chat model, and the visible GPU-admission wait."""
import io
import logging
import threading
import time

import pytest

import server
from sonder_runtime.adapters.inference import startup_prewarm
from sonder_runtime.application.chat import admission_wait_status as wait_status
from sonder_runtime.interfaces.http import live_stream
from sonder_runtime.platform import config as sonder_config
from tests.support.gpu_admission_harness import (
    DIGEST, GIB, MODEL, FakeOllama, chat_body, make_service, wire,
)

LOW_HOST = (12 * GIB, 10 * GIB)


# -- the setting --------------------------------------------------------------

def test_prewarm_on_start_defaults_on():
    assert sonder_config.ServerConfig().prewarm_on_start is True
    assert sonder_config.load_config(env={}).server.prewarm_on_start is True


@pytest.mark.parametrize("raw, expected", [("0", False), ("false", False), ("1", True), ("true", True)])
def test_prewarm_on_start_env(raw, expected):
    config = sonder_config.load_config(env={"SONDER_PREWARM_ON_START": raw})
    assert config.server.prewarm_on_start is expected


def test_prewarm_on_start_config_key(tmp_path):
    path = tmp_path / "sonder.toml"
    path.write_text("[server]\nprewarm_on_start = false\n", encoding="utf-8")
    assert sonder_config.load_config(path, env={}).server.prewarm_on_start is False
    # The environment outranks the file.
    config = sonder_config.load_config(path, env={"SONDER_PREWARM_ON_START": "1"})
    assert config.server.prewarm_on_start is True


def test_prewarm_on_start_rejects_a_non_boolean(tmp_path):
    path = tmp_path / "sonder.toml"
    path.write_text('[server]\nprewarm_on_start = "yes"\n', encoding="utf-8")
    with pytest.raises(sonder_config.ConfigError):
        sonder_config.load_config(path, env={})


# -- the start-up hook --------------------------------------------------------

class _InlineThread:
    def __init__(self, *, target, args=(), name="", daemon=False):
        self._target, self._args = target, args

    def start(self):
        self._target(*self._args)


def _start(setting, prewarm, enabled):
    return startup_prewarm.start_default_model_prewarm(
        setting=setting, prewarm=prewarm, enabled=enabled, thread_factory=_InlineThread)


def test_hook_prewarms_the_default_model_once_when_enabled():
    calls = []
    assert _start(True, lambda tier: calls.append(tier) or True, lambda: True) is True
    assert calls == [""]


def test_hook_does_nothing_when_prewarm_is_disabled():
    calls = []
    assert _start(True, lambda tier: calls.append(tier) or True, lambda: False) is True
    assert calls == []


def test_hook_does_nothing_when_the_setting_is_off():
    calls = []
    assert _start(False, lambda tier: calls.append(tier) or True, lambda: True) is False
    assert calls == []


def test_hook_swallows_a_prewarm_exception(caplog):
    def boom(_tier):
        raise RuntimeError("inventory down")

    with caplog.at_level(logging.INFO, logger="sonder.prewarm"):
        assert _start(True, boom, lambda: True) is True
    assert any("start-up prewarm failed" in r.getMessage() for r in caplog.records)


def test_hook_survives_a_refused_thread():
    def refuse(**_kwargs):
        raise RuntimeError("worker admission closed")

    assert startup_prewarm.start_default_model_prewarm(
        setting=True, prewarm=lambda tier: True, enabled=lambda: True,
        thread_factory=refuse) is False


def test_hook_does_not_block_the_caller():
    release = threading.Event()
    started = threading.Event()

    def slow(_tier):
        started.set()
        release.wait(5)
        return True

    began = time.monotonic()
    assert startup_prewarm.start_default_model_prewarm(
        setting=True, prewarm=slow, enabled=lambda: True,
        thread_factory=lambda **kw: threading.Thread(**kw)) is True
    assert time.monotonic() - began < 1.0
    assert started.wait(2)
    release.set()


def test_hook_is_a_no_op_under_the_suite_default(monkeypatch):
    # pytest.ini sets SONDER_PREWARM=0, so the real switch reads off.
    monkeypatch.setenv("SONDER_PREWARM", "0")
    calls = []
    _start(True, lambda tier: calls.append(tier) or True, server.sonder_speculation.prewarm_enabled)
    assert calls == []


def test_prewarm_model_default_tier_is_the_empty_model_route(monkeypatch):
    # The hook calls prewarm_model(""), which resolves exactly like a chat
    # request with an empty model name: the "sonder" tier.
    seen = []
    monkeypatch.setattr(server.sonder_speculation, "prewarm_enabled", lambda: True)
    monkeypatch.setattr(server._tool_lane_guard, "chat_target",
                        lambda target, tier, extra: seen.append(tier) or ("m", True, False, "cloud"))
    assert server.prewarm_model("") is False  # cloud tier: skipped
    assert seen == ["sonder"]


def test_load_outcome_lines(caplog):
    from sonder_runtime.adapters.inference.gpu_admission import GpuAdmissionRefused
    from sonder_runtime.domain.gpu_admission import RefusalReason, Refused

    with caplog.at_level(logging.INFO, logger="sonder.prewarm"):
        startup_prewarm.load_finished("m1")
        startup_prewarm.load_failed("m1", GpuAdmissionRefused(
            Refused(RefusalReason.HOST_RAM_FLOOR, "4.2 GiB free")))
        startup_prewarm.load_failed("m1", ValueError("x"))
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "m1 loaded" in text
    assert "refused by GPU admission (host_ram_floor): 4.2 GiB free" in text
    assert "not loaded (ValueError)" in text


# -- the visible admission wait -----------------------------------------------

def test_describe_wait_names_the_numbers():
    line = wait_status.describe_wait("qwen", int(4.2 * GIB), int(6.5 * GIB))
    assert line == "waiting for memory to load qwen: 4.2 GiB free, needs ~6.5 GiB"


def test_reporter_emits_at_most_once_per_interval():
    now = [100.0]
    lines = []
    reporter = wait_status.AdmissionWaitReporter(lines.append, interval_seconds=15, clock=lambda: now[0])
    assert reporter.report("a") is True
    now[0] += 5
    assert reporter.report("b") is False
    now[0] += 10
    assert reporter.report("c") is True
    assert lines == ["a", "c"]


def test_reporter_drops_a_failing_sink():
    def bad(_line):
        raise OSError("gone")

    reporter = wait_status.AdmissionWaitReporter(bad, interval_seconds=0)
    assert reporter.report("a") is False
    assert reporter.report("b") is False


def test_report_wait_is_a_no_op_when_nothing_is_armed():
    assert wait_status.report_wait("m", 1, 2) is False


def test_admission_queue_wait_is_reported_and_stops_when_admitted(monkeypatch, tmp_path):
    fake = FakeOllama()
    state = {"available": 12 * GIB}
    service = make_service(tmp_path, fake, host=LOW_HOST, on_host_floor="queue",
                           host_recheck_interval_ms=100)
    domain_reading = type(service._host_reader())
    service._host_reader = lambda: domain_reading(
        available_physical_bytes=state["available"], total_physical_bytes=64 * GIB,
        commit_limit_bytes=96 * GIB, commit_available_bytes=64 * GIB, commit_enforced=True)
    wire(monkeypatch, server, fake, service)
    service.record_footprint(DIGEST, 16384, 17 * GIB, 17 * GIB)
    lines = []
    reporter = wait_status.AdmissionWaitReporter(lines.append, interval_seconds=0)
    threading.Timer(0.35, lambda: state.update(available=40 * GIB)).start()

    with wait_status.armed(reporter):
        server._post_model("/api/chat", chat_body(16384), model=MODEL, timeout=10)

    assert len(fake.chat_bodies) == 1
    assert lines, "a queued load must say why it waits"
    assert all(line.startswith("waiting for memory to load ") and "GiB free, needs ~" in line
               for line in lines)
    assert "12.0 GiB free" in lines[0]
    assert reporter.waiting is False  # admitted: no later wait line
    count = len(lines)
    server._post_model("/api/chat", chat_body(16384), model=MODEL, timeout=10)
    assert len(lines) == count


# -- the SSE side -------------------------------------------------------------

class _Early:
    def __init__(self):
        self.lock = threading.Lock()
        self.client_gone = False


class _Handler:
    def __init__(self, early):
        self._early_stream = early
        self.wfile = io.BytesIO()


def test_wait_line_is_written_as_an_sse_comment():
    handler = _Handler(_Early())
    reporter = live_stream.arm_wait_status(handler, interval_seconds=0)
    with wait_status.armed(reporter):
        wait_status.report_wait("qwen", 4 * GIB, 6 * GIB)
    out = handler.wfile.getvalue().decode()
    assert out.startswith(": sonder-status waiting for memory to load qwen: 4.0 GiB free")
    assert out.endswith("\n\n")


def test_no_reporter_without_an_early_stream():
    assert live_stream.arm_wait_status(_Handler(None)) is None
