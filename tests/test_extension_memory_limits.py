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


def _available_systemd(_argv, **_kwargs):
    return SimpleNamespace(returncode=0, stdout="running\n", stderr="")


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
        if "is-system-running" in argv:
            return SimpleNamespace(returncode=0, stdout="running\n", stderr="")
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


def test_posix_compute_job_fails_closed_when_systemd_binaries_have_no_manager():
    calls = []

    def runner(argv, **_kwargs):
        calls.append(tuple(argv))
        return SimpleNamespace(
            returncode=0,
            stdout='"systemd" is not running in this container\n',
            stderr="",
        )

    limiter = NativeExtensionMemoryLimiter(
        os_module=SimpleNamespace(name="posix", environ={}, geteuid=lambda: 0),
        platform_name="posix",
        which=lambda name: f"/usr/bin/{name}",
        command_runner=runner,
    )

    supported, detail = limiter.process_job_support()
    assert supported is False
    assert "not running" in detail
    with pytest.raises(ExtensionMemoryLimitUnsupported, match="live systemd manager"):
        limiter.prepare_process_job("job-1", ("python",), None, 2)
    assert len(calls) == 2
    assert all(call[-1] == "is-system-running" for call in calls)


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


def test_windows_token_requires_observed_empty_job_before_close(monkeypatch):
    from sonder_runtime.adapters.extensions.memory_limits import _WindowsJobToken

    calls = []
    observations = iter([(2, (258,)), (0, (0,))])
    token = _WindowsJobToken(
        123,
        lambda handle: calls.append("close") or True,
        terminate=lambda handle: calls.append("terminate") or True,
    )
    monkeypatch.setattr(token, "_observe", lambda: next(observations))
    proof = token.quiesce(force=True)
    assert proof.complete and proof.forced
    assert calls == ["terminate"]
    token.close()
    assert calls == ["terminate", "close"]


def test_windows_token_query_failure_does_not_claim_empty_or_drop_handle(monkeypatch):
    from sonder_runtime.adapters.extensions.memory_limits import _WindowsJobToken

    def failed_query():
        raise ExtensionMemoryLimitError("query failure")

    token = _WindowsJobToken(
        123,
        lambda handle: True,
        terminate=lambda handle: True,
    )
    monkeypatch.setattr(token, "_observe", failed_query)
    assert not token.quiesce(force=True).complete
    with pytest.raises(Exception, match="quiescent"):
        token.close()
    assert token._handle == 123


@pytest.mark.parametrize("user_scope", [True, False])
def test_isolated_scope_bus_context_is_wrapper_only(user_scope):
    limiter = NativeExtensionMemoryLimiter(
        os_module=SimpleNamespace(name="posix", environ={"DBUS_SESSION_BUS_ADDRESS": "malicious", "SECRET": "private"}, geteuid=lambda: 1000),
        platform_name="posix", which=lambda name: f"/usr/bin/{name}",
        command_runner=_available_systemd, systemd_user=user_scope)
    argv = ("/usr/bin/python3", "-c", "pass")
    prepared = limiter.prepare_process_job("isolated", argv, 1024 * 1024, 3)
    environment = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    result = limiter.isolated_process_environment(prepared, argv, environment)
    assert result.token is prepared.token
    if not user_scope:
        assert result.argv[-7:] == ("/usr/bin/env", "-u", "INVOCATION_ID", "--", *argv)
        assert result.launch_options["env"] == environment
        return
    assert result.argv[-9:] == ("/usr/bin/env", "-u", "DBUS_SESSION_BUS_ADDRESS", "-u", "INVOCATION_ID", "--", *argv)
    wrapper = result.launch_options["env"]
    assert "SECRET" not in wrapper
    assert "malicious" not in result.argv
    assert environment == {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    if user_scope:
        assert wrapper["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/run/user/1000/bus"
    else:
        assert wrapper == environment


@pytest.mark.parametrize("key", ["LD_PRELOAD", "LD_LIBRARY_PATH", "GLIBC_TUNABLES", "SECRET_TOKEN", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR", "SYSTEMD_UNIT_PATH"])
def test_isolated_scope_rejects_unsupported_environment_without_values_in_argv(key):
    limiter = NativeExtensionMemoryLimiter(
        os_module=SimpleNamespace(name="posix", environ={}, geteuid=lambda: 1000),
        platform_name="posix", which=lambda name: f"/usr/bin/{name}",
        command_runner=_available_systemd)
    argv = ("/usr/bin/python3", "-c", "pass")
    prepared = limiter.prepare_process_job("unsupported-env", argv, 1024 * 1024, 3)
    with pytest.raises(ExtensionMemoryLimitUnsupported, match="unsupported keys"):
        limiter.isolated_process_environment(prepared, argv, {key: "secret-must-never-enter-argv"})
    assert "secret-must-never-enter-argv" not in " ".join(prepared.argv)


class NprocResource:
    RLIMIT_AS = 9
    RLIMIT_NPROC = 6
    RLIM_INFINITY = -1

    def __init__(self, current=(-1, -1)):
        self.current = current
        self.set = []

    def getrlimit(self, which):
        assert which == self.RLIMIT_NPROC
        return self.current

    def setrlimit(self, which, limits):
        self.set.append((which, limits))


def _proc_tree(root, processes):
    (root / "self" / "task").mkdir(parents=True)
    for pid, (uid, threads) in processes.items():
        (root / str(pid)).mkdir()
        (root / str(pid) / "status").write_text(
            "Name:\tx\nUid:\t%d\t%d\t%d\t%d\nThreads:\t%d\n" % (uid, uid, uid, uid, threads))
    (root / "not-a-pid").mkdir()


def _nproc_limiter(resource, uid=1000, **kwargs):
    return NativeExtensionMemoryLimiter(
        os_module=SimpleNamespace(name="posix", getuid=lambda: uid),
        resource_module=resource, platform_name="posix", **kwargs)


def test_posix_nproc_is_counted_from_the_uids_tasks_not_set_to_the_bare_descendant_cap(
        tmp_path, monkeypatch):
    # RLIMIT_NPROC is charged per real uid (threads included on Linux). A
    # bare descendant cap starved every fork whenever the runtime's uid
    # already ran that many tasks, as on a CI runner whose agent shares it.
    import sonder_runtime.adapters.extensions.memory_limits as memory_limits

    _proc_tree(tmp_path, {10: (1000, 30), 11: (1000, 1), 12: (0, 90), 13: (1001, 5)})
    monkeypatch.setattr(memory_limits, "_PROC_ROOT", str(tmp_path))
    resource = NprocResource()
    options = _nproc_limiter(resource).launch_options(4 << 30, 9)
    options["preexec_fn"]()
    # 31 tasks of uid 1000: room for the uid's own growth plus the 9 the job may add.
    assert resource.set == [(9, (4 << 30, 4 << 30)), (6, (2 * 31 + 9, 2 * 31 + 9))]


def test_posix_nproc_never_exceeds_the_runtimes_own_limits(tmp_path, monkeypatch):
    import sonder_runtime.adapters.extensions.memory_limits as memory_limits

    _proc_tree(tmp_path, {10: (1000, 30)})
    monkeypatch.setattr(memory_limits, "_PROC_ROOT", str(tmp_path))
    resource = NprocResource(current=(50, 80))
    _nproc_limiter(resource).launch_options(None, 9)["preexec_fn"]()
    assert resource.set == [(6, (50, 50))]


def test_posix_nproc_counts_processes_with_ps_without_proc(tmp_path, monkeypatch):
    # BSD and macOS charge processes, not threads, and have no /proc.
    import sonder_runtime.adapters.extensions.memory_limits as memory_limits

    monkeypatch.setattr(memory_limits, "_PROC_ROOT", str(tmp_path / "absent"))
    runs = []

    def runner(argv, **kwargs):
        runs.append(tuple(argv))
        return SimpleNamespace(returncode=0, stdout="  0\n1000\n 1000\n1001\n1000\n", stderr="")

    resource = NprocResource()
    _nproc_limiter(resource, which=lambda name: "/bin/ps", command_runner=runner).launch_options(
        None, 5)["preexec_fn"]()
    assert runs == [("/bin/ps", "-A", "-o", "ruid=")]
    assert resource.set == [(6, (2 * 3 + 5, 2 * 3 + 5))]


def test_posix_nproc_refuses_when_the_uid_task_count_is_unknown(tmp_path, monkeypatch):
    import sonder_runtime.adapters.extensions.memory_limits as memory_limits

    monkeypatch.setattr(memory_limits, "_PROC_ROOT", str(tmp_path / "absent"))
    failing = SimpleNamespace(returncode=1, stdout="", stderr="no ps")
    for limiter in (
        _nproc_limiter(NprocResource(), which=lambda name: None),
        _nproc_limiter(NprocResource(), which=lambda name: "/bin/ps",
                       command_runner=lambda *args, **kwargs: failing),
        NativeExtensionMemoryLimiter(os_module=SimpleNamespace(name="posix"),
                                     resource_module=NprocResource(), platform_name="posix"),
    ):
        with pytest.raises(ExtensionMemoryLimitUnsupported, match="per-uid task count"):
            limiter.launch_options(None, 5)
