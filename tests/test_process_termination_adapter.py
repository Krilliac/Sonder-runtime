from types import SimpleNamespace

from sonder_runtime.adapters import process_termination


class _Proc:
    pid = 42

    def __init__(self, running=True):
        self.running = running
        self.killed = False

    def poll(self):
        return None if self.running else 0

    def kill(self):
        self.killed = True


def test_windows_teardown_uses_taskkill_tree(monkeypatch):
    calls = []
    fake_subprocess = SimpleNamespace(
        DEVNULL=object(),
        SubprocessError=Exception,
        run=lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    fake_os = SimpleNamespace(name="nt")
    proc = _Proc()

    process_termination.terminate_process_tree(
        proc, os_module=fake_os, subprocess_module=fake_subprocess
    )

    assert calls[0][0][0] == ["taskkill", "/PID", "42", "/T", "/F"]
    assert proc.killed is False


def test_posix_teardown_kills_process_group(monkeypatch):
    calls = []
    fake_os = SimpleNamespace(
        name="posix", killpg=lambda pid, sig: calls.append((pid, sig))
    )
    fake_signal = SimpleNamespace(SIGKILL="SIGKILL")
    proc = _Proc()

    process_termination.terminate_process_tree(
        proc, os_module=fake_os, signal_module=fake_signal
    )

    assert calls == [(42, "SIGKILL")]
    assert proc.killed is False


def test_teardown_falls_back_to_process_kill_when_group_teardown_fails():
    fake_os = SimpleNamespace(
        name="posix", killpg=lambda pid, sig: (_ for _ in ()).throw(OSError())
    )
    proc = _Proc()

    process_termination.terminate_process_tree(
        proc, os_module=fake_os, signal_module=SimpleNamespace(SIGKILL="SIGKILL")
    )

    assert proc.killed is True


def test_already_finished_process_is_untouched():
    proc = _Proc(running=False)

    process_termination.terminate_process_tree(proc)

    assert proc.killed is False
