"""mono_ns must be read from the clock the discovery document advertises.

Discovery says ``"mono_ns": "host-monotonic"``: the same clock every producer
on this host uses, so Observatory can merge Runtime and Inference events by
``mono_ns``.  Inference (MSVC ``steady_clock``) reads QueryPerformanceCounter;
Windows CPython before 3.13 implements ``time.monotonic_ns`` with
GetTickCount64 instead -- 15.6 ms steps and a tens-of-ms offset from QPC --
which ordered Runtime events before the Inference events they follow.
"""
import json
import sys
import time
from datetime import datetime, timezone

import pytest

from sonder_runtime.adapters.observability import observatory_producer as op
from sonder_runtime.application.ports.telemetry_sink import TelemetryEvent


def test_default_clock_is_the_host_monotonic_clock():
    expected = time.perf_counter_ns if sys.platform == "win32" else time.monotonic_ns
    assert op.host_monotonic_ns is expected


@pytest.mark.skipif(sys.platform != "win32", reason="QPC is the Windows host clock")
def test_windows_mono_ns_is_query_performance_counter():
    producer = op.ObservatoryProducer(version="1", node_id="h", instance_hex="0123456789ab")
    before = time.perf_counter_ns()
    producer.emit(TelemetryEvent("request.started", datetime.now(timezone.utc), {"n": 1},
                                 correlation_id="r", run_id="r"))
    after = time.perf_counter_ns()
    event = producer.subscribe().next_batch(0.05, limit=8).events[0]
    mono_ns = json.loads(event.line)["mono_ns"]
    assert before <= mono_ns <= after
