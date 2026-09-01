"""EXT-003 native memory-limit contract tests."""

import sys
from types import SimpleNamespace

import pytest

from sonder_runtime.adapters.extensions.host import ExtensionHost, ExtensionHostLimits
from sonder_runtime.adapters.extensions.memory_limits import (
    ExtensionMemoryLimitError,
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
    if sys.platform != "win32":
        pytest.skip("The host platform has a native POSIX adapter")
    with pytest.raises(ExtensionMemoryLimitUnsupported, match="unsupported"):
        NativeExtensionMemoryLimiter(
            os_module=SimpleNamespace(name="plan9"), platform_name="plan9"
        ).apply(object(), 1024)


def test_posix_native_limiter_applies_hard_address_space_limit():
    calls = []

    class FakeResource:
        RLIMIT_AS = 9

        @staticmethod
        def prlimit(pid, resource, limits):
            calls.append((pid, resource, limits))

    token = NativeExtensionMemoryLimiter(
        os_module=SimpleNamespace(name="posix"),
        resource_module=FakeResource,
        platform_name="posix",
    ).apply(SimpleNamespace(pid=1234), 1024)
    assert calls == [(1234, 9, (1024, 1024))]
    token.close()


def test_posix_compute_job_uses_systemd_scope_for_aggregate_limits():
    calls = []
    states = iter(("active", "inactive", "inactive"))

    def runner(argv, **_kwargs):
        calls.append(tuple(argv))
        if "show" in argv:
            return SimpleNamespace(returncode=0, stdout=next(states) + "\n", stderr="")
        if "kill" in argv:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(argv)

    limiter = NativeExtensionMemoryLimiter(
        os_module=SimpleNamespace(
            name="posix",
            environ={},
            geteuid=lambda: 1000,
        ),
        platform_name="posix",
        which=lambda name: f"/usr/bin/{name}",
        command_runner=runner,
        sleeper=lambda _seconds: None,
    )
    prepared = limiter.prepare_process_job(
        "job-1",
        ("python", "-c", "pass"),
        128 * 1024 * 1024,
        5,
    )

    assert prepared.argv[0:5] == (
        "/usr/bin/systemd-run",
        "--user",
        "--no-ask-password",
        "--scope",
        "--quiet",
    )
    assert "--property=TasksMax=5" in prepared.argv
    assert "--property=MemoryMax=134217728" in prepared.argv
    assert prepared.argv[-4:] == ("--", "python", "-c", "pass")
    result = prepared.token.quiesce(force=True)
    assert result.complete is True
    assert result.forced is True
    assert any("kill" in call for call in calls)
    prepared.token.close()


def test_posix_compute_job_fails_closed_without_systemd_scope_tools():
    limiter = NativeExtensionMemoryLimiter(
        os_module=SimpleNamespace(name="posix", environ={}, geteuid=lambda: 1000),
        platform_name="posix",
        which=lambda _name: None,
    )
    with pytest.raises(ExtensionMemoryLimitUnsupported, match="systemd"):
        limiter.prepare_process_job("job-1", ("python",), None, 2)


def test_restored_systemd_scope_must_belong_to_the_exact_job():
    limiter = NativeExtensionMemoryLimiter(
        os_module=SimpleNamespace(name="posix", environ={}, geteuid=lambda: 1000),
        platform_name="posix",
        which=lambda name: f"/usr/bin/{name}",
    )
    metadata = {
        "containment_kind": "systemd_scope",
        "containment_unit": "sonder-compute-0123456789abcdefabcd.scope",
        "containment_user": "1",
    }

    with pytest.raises(ExtensionMemoryLimitError, match="does not belong"):
        limiter.restore_process_job("job-a", metadata)


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
