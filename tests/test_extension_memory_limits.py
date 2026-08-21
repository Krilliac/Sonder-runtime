"""EXT-003 native memory-limit contract tests."""

import sys

import pytest

from sonder_runtime.adapters.extensions.host import ExtensionHost, ExtensionHostLimits
from sonder_runtime.adapters.extensions.memory_limits import (
    ExtensionMemoryLimitUnsupported,
    NativeExtensionMemoryLimiter,
)


READY = 'import json,sys\nprint(json.dumps({"type":"ready"}), flush=True)\n'
ECHO = READY + 'for line in sys.stdin:\n r=json.loads(line)\n print(json.dumps({"id":r["id"]}), flush=True)'


class Token:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeLimiter:
    def __init__(self):
        self.calls = []
        self.token = Token()

    def apply(self, process, limit_bytes):
        self.calls.append((process, limit_bytes))
        return self.token


def test_requested_limit_is_applied_before_ready_and_closed_with_process():
    limiter = FakeLimiter()
    host = ExtensionHost(
        [sys.executable, "-c", ECHO],
        limits=ExtensionHostLimits(memory_limit_bytes=32 * 1024 * 1024),
        memory_limiter=limiter,
    )
    try:
        assert host.call("ping") == {"id": 1}
        assert limiter.calls[0][1] == 32 * 1024 * 1024
    finally:
        host.close()
    assert limiter.token.closed


def test_native_limiter_is_truthfully_unsupported_off_windows():
    if sys.platform == "win32":
        pytest.skip("Windows has the native Job Object adapter")
    with pytest.raises(ExtensionMemoryLimitUnsupported, match="unsupported"):
        NativeExtensionMemoryLimiter().apply(object(), 1024)


def test_windows_native_limiter_attaches_to_live_extension():
    if sys.platform != "win32":
        pytest.skip("Windows Job Objects are required")
    host = ExtensionHost(
        [sys.executable, "-c", ECHO],
        limits=ExtensionHostLimits(memory_limit_bytes=256 * 1024 * 1024),
    )
    try:
        assert host.call("ping") == {"id": 1}
    finally:
        host.close()


def test_memory_limit_requires_positive_integer():
    with pytest.raises(ValueError, match="memory_limit_bytes"):
        ExtensionHostLimits(memory_limit_bytes=0)
    with pytest.raises(ValueError, match="memory_limit_bytes"):
        ExtensionHostLimits(memory_limit_bytes=True)
