"""Focused EXT-003 tests for the bounded JSON-lines extension host."""

import sys
import time

import pytest

from sonder_runtime.adapters.extensions.host import (
    ExtensionHost,
    ExtensionHostCrashed,
    ExtensionHostLimits,
    ExtensionHostOutputLimit,
    ExtensionHostProtocolError,
    ExtensionHostTimeout,
)


READY = 'import json,sys\nprint(json.dumps({"type":"ready"}), flush=True)\n'
ECHO_SERVER = READY + 'for line in sys.stdin:\n r=json.loads(line)\n print(json.dumps({"id":r["id"],"ok":r["params"]}), flush=True)'
HANG_SERVER = READY + 'import time\nfor line in sys.stdin:\n time.sleep(30)'
LARGE_SERVER = READY + 'for line in sys.stdin:\n print("x" * 1000, flush=True)'
CRASH_SERVER = READY + 'next(sys.stdin)\nraise SystemExit(7)'


def _host(source, *, limits=None):
    return ExtensionHost([sys.executable, "-c", source], limits=limits)


def test_json_lines_call_returns_matching_response_and_tracks_stats():
    host = _host(ECHO_SERVER)
    try:
        assert host.call("echo", {"value": "bounded"}) == {"id": 1, "ok": {"value": "bounded"}}
        assert host.stats.calls == 1
        assert host.stats.launches == 1
    finally:
        host.close()


def test_startup_timeout_terminates_child():
    host = _host(
        'import time; time.sleep(30)',
        limits=ExtensionHostLimits(startup_timeout_seconds=0.05, max_restarts=0),
    )
    with pytest.raises(ExtensionHostTimeout, match="bounded timeout"):
        host.start()
    host.close()


def test_call_timeout_restarts_without_replaying_side_effecting_call():
    host = _host(
        HANG_SERVER,
        limits=ExtensionHostLimits(call_timeout_seconds=0.05, max_restarts=1),
    )
    try:
        with pytest.raises(ExtensionHostTimeout):
            host.call("hang")
        assert host.stats.restarts == 1
        with pytest.raises(ExtensionHostTimeout):
            host.call("hang-again")
        assert host.stats.restarts == 1
    finally:
        host.close()


def test_output_byte_bound_rejects_oversized_response_and_recovers():
    host = _host(
        LARGE_SERVER,
        limits=ExtensionHostLimits(max_output_bytes=64, max_restarts=1),
    )
    try:
        with pytest.raises(ExtensionHostOutputLimit, match="output exceeds"):
            host.call("large")
        assert host.stats.restarts == 1
    finally:
        host.close()


def test_crash_budget_stops_recovery_after_configured_crashes():
    host = _host(
        CRASH_SERVER,
        limits=ExtensionHostLimits(call_timeout_seconds=0.5, max_restarts=3, max_crashes=1),
    )
    try:
        with pytest.raises(ExtensionHostCrashed):
            host.call("crash")
        assert host.stats.crashes == 1
        with pytest.raises(ExtensionHostCrashed, match="crash budget|not running"):
            host.call("crash-again")
        assert host.stats.crashes == 2
    finally:
        host.close()


def test_protocol_requires_ready_handshake_and_matching_response_id():
    bad_ready = _host('print("not-json", flush=True)')
    with pytest.raises(ExtensionHostProtocolError):
        bad_ready.start()
    bad_ready.close()

    mismatched = _host(READY + 'print("{\\"id\\":99}", flush=True)')
    try:
        with pytest.raises(ExtensionHostProtocolError, match="response id"):
            mismatched.call("m")
    finally:
        mismatched.close()
