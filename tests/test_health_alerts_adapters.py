import json
import logging
import subprocess
import urllib.error
from datetime import datetime

import pytest

from sonder_runtime.adapters.health_alerts.notifiers import (
    FanoutNotifier,
    LogNotifier,
    NtfyNotifier,
    ToastNotifier,
)
from sonder_runtime.adapters.health_alerts.schtasks import SchtasksReader
from sonder_runtime.adapters.health_alerts.state_store import JsonAlertStateStore
from sonder_runtime.application.ports.health_alerts import DeliveryResult, TaskUnavailable
from sonder_runtime.domain.health_alerts import Alert, AlertKind

ALERT = Alert(AlertKind.DOCTOR_CHECK, "ollama", "Title \x07 here", "Detail line")
SECRET_URL = "https://ntfy.example.invalid/topic-s3cr3t"


class Done:
    def __init__(self, returncode=0, stdout=""):
        self.returncode, self.stdout = returncode, stdout


SECRET_ENV = {
    "SONDER_HEALTH_ALERT_NTFY_URL": SECRET_URL,
    "SONDER_AUTH_SECRET": "auth-s3cr3t",
    "SONDER_BACKUP_KEY_FILE": "backup-key",
    "OPENROUTER_API_KEY": "or-key",
    "SONDER_OPENAI_API_KEY": "oa-key",
}


@pytest.fixture
def system_env(monkeypatch):
    monkeypatch.setenv("SystemRoot", "C:\\Windows")
    for key, value in SECRET_ENV.items():
        monkeypatch.setenv(key, value)


def _assert_no_secrets(env):
    assert not set(SECRET_ENV) & set(env)
    assert not set(SECRET_ENV.values()) & set(env.values())


def test_toast_passes_text_through_environment_not_script(system_env):
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw))
        return Done(0)

    result = ToastNotifier(timeout_seconds=5, run=run, platform="win32").send(ALERT)
    assert result == (DeliveryResult("toast", True),)
    argv, kw = calls[0]
    assert argv[0].replace("\\", "/") == "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
    assert "-ExecutionPolicy" not in argv
    _assert_no_secrets(kw["env"])
    assert "Detail line" not in " ".join(argv)
    assert kw["env"]["SONDER_ALERT_DETAIL"] == "Detail line"
    assert "\x07" not in kw["env"]["SONDER_ALERT_TITLE"]
    assert kw["timeout"] == 5


@pytest.mark.parametrize("fault", [OSError("x"), subprocess.TimeoutExpired("p", 1)])
def test_toast_failure_is_a_result_not_an_exception(fault, system_env):
    def run(argv, **kw):
        raise fault

    (result,) = ToastNotifier(timeout_seconds=1, run=run, platform="win32").send(ALERT)
    assert not result.delivered and result.error


def test_toast_nonzero_exit_is_undelivered(system_env):
    (result,) = ToastNotifier(timeout_seconds=1, run=lambda *a, **k: Done(1), platform="win32").send(ALERT)
    assert not result.delivered and "1" in result.error


def test_toast_off_windows_is_undelivered_without_running_anything():
    def run(*a, **k):
        raise AssertionError("must not run")

    (result,) = ToastNotifier(timeout_seconds=1, run=run, platform="linux").send(ALERT)
    assert not result.delivered


def test_ntfy_posts_text_with_title_header():
    seen = {}

    def post(url, body, headers, timeout):
        seen.update(url=url, body=body, headers=dict(headers), timeout=timeout)
        return 200

    (result,) = NtfyNotifier(SECRET_URL, timeout_seconds=3, post=post).send(ALERT)
    assert result.delivered
    assert seen["url"] == SECRET_URL and seen["body"] == b"Detail line"
    assert seen["headers"]["Title"].isascii() and "\x07" not in seen["headers"]["Title"]


@pytest.mark.parametrize("fault", [
    urllib.error.URLError("boom " + SECRET_URL),
    ConnectionError("to " + SECRET_URL),
    urllib.error.HTTPError(SECRET_URL, 503, "x", {}, None),
])
def test_ntfy_failure_never_reveals_the_url(fault):
    def post(*a):
        raise fault

    notifier = NtfyNotifier(SECRET_URL, timeout_seconds=1, post=post)
    (result,) = notifier.send(ALERT)
    assert not result.delivered
    assert "s3cr3t" not in repr(result) and "s3cr3t" not in repr(notifier)


def test_ntfy_non_2xx_status_is_undelivered():
    (result,) = NtfyNotifier(SECRET_URL, timeout_seconds=1, post=lambda *a: 404).send(ALERT)
    assert not result.delivered and result.error == "http 404"


def test_log_notifier_logs_error_without_secrets(caplog):
    with caplog.at_level(logging.ERROR):
        (result,) = LogNotifier().send(ALERT)
    assert result.delivered and "doctor_check:ollama" in caplog.text


class Stub:
    def __init__(self, ok):
        self.ok, self.calls = ok, 0

    def send(self, alert):
        self.calls += 1
        return (DeliveryResult("stub", self.ok),)


def test_fanout_uses_log_fallback_only_when_nothing_delivered():
    fallback = Stub(True)
    results = FanoutNotifier([Stub(True), Stub(False)], fallback).send(ALERT)
    assert fallback.calls == 0 and any(r.delivered for r in results)
    fallback = Stub(True)
    results = FanoutNotifier([Stub(False)], fallback).send(ALERT)
    assert fallback.calls == 1 and results[-1].delivered


def test_fanout_with_no_channels_still_logs():
    results = FanoutNotifier([]).send(ALERT)
    assert [r.channel for r in results] == ["log"]


CSV = (
    '"HostName","TaskName","Next Run Time","Status","Logon Mode","Last Run Time","Last Result",'
    '"Author","Task To Run","Scheduled Task State"\n'
    '"H","\\JobA","N/A","Ready","x","11/14/2026 9:15:01 PM","0","a","c","Enabled"\n'
)


def test_toast_without_systemroot_never_runs_a_bare_powershell(monkeypatch):
    monkeypatch.delenv("SystemRoot", raising=False)
    monkeypatch.delenv("WINDIR", raising=False)

    def run(*a, **k):
        raise AssertionError("must not run")

    (result,) = ToastNotifier(timeout_seconds=1, run=run, platform="win32").send(ALERT)
    assert not result.delivered and result.error


def test_schtasks_without_systemroot_never_runs_a_bare_schtasks(monkeypatch):
    monkeypatch.delenv("SystemRoot", raising=False)
    monkeypatch.delenv("WINDIR", raising=False)

    def run(*a, **k):
        raise AssertionError("must not run")

    with pytest.raises(TaskUnavailable):
        SchtasksReader(timeout_seconds=1, run=run).read("JobA")


def test_schtasks_reader_builds_argv_without_shell_and_parses(system_env):
    seen = {}

    def run(argv, **kw):
        seen.update(argv=argv, kw=kw)
        return Done(0, CSV)

    state = SchtasksReader(timeout_seconds=4, run=run).read("JobA")
    assert seen["argv"][0].replace("\\", "/") == "C:/Windows/System32/schtasks.exe"
    assert seen["argv"][1:] == ["/query", "/tn", "JobA", "/fo", "csv", "/v"]
    _assert_no_secrets(seen["kw"]["env"])
    assert "shell" not in seen["kw"] and seen["kw"]["timeout"] == 4
    assert state.last_run == datetime(2026, 11, 14, 21, 15, 1) and state.last_result == 0


@pytest.mark.parametrize("name", ["", "-x", "a;b", "a|b", "a\nb"])
def test_schtasks_reader_refuses_odd_names(name, system_env):
    def run(*a, **k):
        raise AssertionError("must not run")

    with pytest.raises(TaskUnavailable):
        SchtasksReader(timeout_seconds=1, run=run).read(name)


@pytest.mark.parametrize("outcome", [Done(1, ""), Done(0, "garbage"), OSError("x"),
                                     subprocess.TimeoutExpired("s", 1)])
def test_schtasks_reader_failures_are_typed(outcome, system_env):
    def run(*a, **k):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    with pytest.raises(TaskUnavailable):
        SchtasksReader(timeout_seconds=1, run=run).read("JobA")


def test_state_store_round_trip_and_atomic_file(tmp_path):
    store = JsonAlertStateStore(tmp_path / "sub" / "state.json")
    assert store.load() == {}
    store.save({"a": 1.5})
    assert store.load() == {"a": 1.5}
    assert not list((tmp_path / "sub").glob("*.tmp"))


@pytest.mark.parametrize("content", ["not json", "[1]", '{"a": "x", "b": true, "c": 2}'])
def test_state_store_tolerates_damaged_content(tmp_path, content):
    path = tmp_path / "state.json"
    path.write_text(content, encoding="utf-8")
    loaded = JsonAlertStateStore(path).load()
    assert loaded in ({}, {"c": 2.0})
    json.dumps(loaded)
