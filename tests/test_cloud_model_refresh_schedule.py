"""The daily refresh trigger: min interval, retry back-off, switches, fail-soft, hook wiring."""

import ast
import json
import logging
import os
import threading
from pathlib import Path

import pytest

import sonder_runtime.adapters.cloud_model_refresh as refresh
import sonder_runtime.adapters.cloud_model_refresh_schedule as schedule
from sonder_runtime.domain import cloud_model_refresh as crm

NOW = 1_800_000_000.0
DAY = 24 * 3600
REPO = Path(__file__).resolve().parent.parent


def _state(age_seconds):
    return {"version": 1, "last_success_at": crm.iso_timestamp(NOW - age_seconds)}


# --- 24. due() -----------------------------------------------------------------------


def test_never_refreshed_is_due():
    assert schedule.due(None, NOW, None)
    assert schedule.due({"version": 1}, NOW, None)
    assert schedule.due({"version": 1, "last_success_at": "garbage"}, NOW, None)


def test_min_interval_is_one_day_inclusive_boundary():
    assert schedule.MIN_INTERVAL_SECONDS == DAY
    assert not schedule.due(_state(60), NOW, None)
    assert not schedule.due(_state(DAY - 1), NOW, None)
    assert schedule.due(_state(DAY), NOW, None)
    assert schedule.due(_state(3 * DAY), NOW, None)


def test_no_retry_within_six_hours_of_a_failed_attempt():
    assert schedule.RETRY_SECONDS == 6 * 3600
    assert not schedule.due(None, NOW, NOW - 3600)
    assert not schedule.due(_state(5 * DAY), NOW, NOW - schedule.RETRY_SECONDS + 1)
    assert schedule.due(_state(5 * DAY), NOW, NOW - schedule.RETRY_SECONDS)


def test_enabled_switches():
    on, off = crm.Preferences(), crm.Preferences(auto_refresh=False)
    assert schedule.enabled({}, None) and schedule.enabled({}, on)
    assert not schedule.enabled({}, off)
    for value in ("0", "false", "No", "OFF", " 0 "):
        assert not schedule.enabled({schedule.ENV_SWITCH: value}, on)
    assert schedule.enabled({schedule.ENV_SWITCH: "1"}, on)


# --- _check_once reads the persisted timestamp ---------------------------------------


class Runner:
    def __init__(self, outcome="ok"):
        self.calls = []
        self.outcome = outcome

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return {"outcome": self.outcome, "reason": None}


def _check(tmp_path, runner, *, last_failure=None, env=None):
    return schedule._check_once(origin="http://127.0.0.1:1", home=tmp_path, env={} if env is None else env,
                                last_failure=last_failure, clock=lambda: NOW, runner=runner)


def _write_state(home, age):
    (home / refresh.STATE_FILE).write_text(json.dumps(_state(age)), encoding="utf-8")


def test_check_reads_last_success_from_the_state_file(tmp_path):
    runner = Runner()
    _write_state(tmp_path, 3600)
    assert _check(tmp_path, runner) is None and runner.calls == []
    _write_state(tmp_path, DAY + 60)
    _check(tmp_path, runner)
    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call["apply"] is True and call["trigger"] == "schedule" and call["home"] == tmp_path


def test_check_with_no_state_runs_and_failures_back_off_while_busy_does_not(tmp_path):
    failing = Runner("aborted")
    assert _check(tmp_path, failing) == NOW  # remembered as the failure time
    assert _check(tmp_path, failing, last_failure=NOW - 60) == NOW - 60 and len(failing.calls) == 1
    assert _check(tmp_path, Runner("busy"), last_failure=None) is None
    assert _check(tmp_path, Runner("ok"), last_failure=NOW - schedule.RETRY_SECONDS) is None


def test_check_respects_the_switches_and_never_runs_when_off(tmp_path):
    runner = Runner()
    _check(tmp_path, runner, env={schedule.ENV_SWITCH: "0"})
    (tmp_path / refresh.PREFERENCES_FILE).write_text(json.dumps({"auto_refresh": False}), encoding="utf-8")
    _check(tmp_path, runner)
    assert runner.calls == []


# --- 25. start_scheduled_refresh -----------------------------------------------------


class FakeThread:
    created = []

    def __init__(self, **kwargs):
        self.kwargs, self.started = kwargs, False
        FakeThread.created.append(self)

    def start(self):
        self.started = True


@pytest.fixture(autouse=True)
def _reset_threads():
    FakeThread.created = []


def test_hermetic_default_keeps_the_thread_off_in_every_test():
    assert os.environ["SONDER_CLOUD_MODEL_REFRESH"] == "0"

    def boom(**_kw):
        pytest.fail("no thread may be created under the test default")

    assert schedule.start_scheduled_refresh(origin="http://127.0.0.1:1", thread_factory=boom) is False


def test_env_switch_off_and_auto_refresh_false_start_no_thread(tmp_path):
    assert not schedule.start_scheduled_refresh(origin="o", thread_factory=FakeThread, home=tmp_path,
                                                env={schedule.ENV_SWITCH: "0"})
    (tmp_path / refresh.PREFERENCES_FILE).write_text(json.dumps({"auto_refresh": False}), encoding="utf-8")
    assert not schedule.start_scheduled_refresh(origin="o", thread_factory=FakeThread, home=tmp_path, env={})
    assert FakeThread.created == []


def test_when_on_one_named_daemon_thread_is_started(tmp_path):
    stop = threading.Event()
    assert schedule.start_scheduled_refresh(origin="http://o", thread_factory=FakeThread, home=tmp_path,
                                            env={}, stop=stop)
    (thread,) = FakeThread.created
    assert thread.started
    assert thread.kwargs["daemon"] is True and thread.kwargs["name"] == "sonder-cloud-model-refresh"
    assert thread.kwargs["target"] is schedule._loop
    assert thread.kwargs["kwargs"] == {"origin": "http://o", "home": tmp_path, "env": {}, "stop": stop}


def test_a_broken_thread_factory_never_raises(tmp_path, caplog):
    def broken(**_kw):
        raise RuntimeError("cannot start")

    with caplog.at_level(logging.WARNING):
        assert schedule.start_scheduled_refresh(origin="o", thread_factory=broken, home=tmp_path, env={}) is False
    assert "did not start" in caplog.text


def _loop(tmp_path, runner, monkeypatch, stop=None):
    monkeypatch.setattr(schedule, "FIRST_DELAY_SECONDS", 0)
    monkeypatch.setattr(schedule, "RECHECK_SECONDS", None)  # one pass, then the loop returns
    schedule._loop(origin="http://127.0.0.1:1", home=tmp_path, env={}, stop=stop or threading.Event(),
                   clock=lambda: NOW, runner=runner)


def test_loop_runs_a_scheduled_apply_logs_only_and_keeps_stdout_clean(tmp_path, monkeypatch, capsys, caplog):
    runner = Runner()
    with caplog.at_level(logging.INFO):
        _loop(tmp_path, runner, monkeypatch)
    assert len(runner.calls) == 1 and runner.calls[0]["apply"] is True and runner.calls[0]["trigger"] == "schedule"
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""  # MCP stdio: stdout is the protocol channel
    assert "scheduled cloud model refresh: ok" in caplog.text


def test_loop_swallows_runner_exceptions_and_logs_them(tmp_path, monkeypatch, capsys, caplog):
    def explode(**_kw):
        raise RuntimeError("boom")

    with caplog.at_level(logging.WARNING):
        _loop(tmp_path, explode, monkeypatch)
    assert "scheduled cloud model refresh failed" in caplog.text
    assert capsys.readouterr().out == ""


def test_loop_exits_without_running_when_stopped_during_the_first_delay(tmp_path, monkeypatch):
    runner = Runner()
    stop = threading.Event()
    stop.set()
    monkeypatch.setattr(schedule, "FIRST_DELAY_SECONDS", 3600)
    schedule._loop(origin="o", home=tmp_path, env={}, stop=stop, clock=lambda: NOW, runner=runner)
    assert runner.calls == []


def test_loop_does_not_run_a_fresh_state_again(tmp_path, monkeypatch):
    runner = Runner()
    _write_state(tmp_path, 600)
    _loop(tmp_path, runner, monkeypatch)
    assert runner.calls == []


def test_loop_default_runner_is_the_real_refresh_and_a_failed_run_stays_soft(tmp_path, monkeypatch, capsys):
    """Unreachable Ollama through the real run_refresh: aborted, zero writes, no exception, no output."""
    monkeypatch.setenv("SONDER_MODEL_LISTS", str(tmp_path / "model_lists.json"))
    monkeypatch.setattr(schedule, "FIRST_DELAY_SECONDS", 0)
    monkeypatch.setattr(schedule, "RECHECK_SECONDS", None)
    schedule._loop(origin="http://127.0.0.1:1", home=tmp_path, env={}, stop=threading.Event(), clock=lambda: NOW)
    assert not (tmp_path / "model_lists.json").exists() and not (tmp_path / refresh.STATE_FILE).exists()
    assert capsys.readouterr().out == ""


# --- 26. hook wiring -----------------------------------------------------------------


def _calls(tree, name):
    return sorted(
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (getattr(node.func, "attr", None) == name or getattr(node.func, "id", None) == name))


def test_serve_starts_the_schedule_after_binding_and_prewarm_with_owned_threads():
    path = REPO / "sonder_runtime" / "interfaces" / "http" / "serve.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    (prewarm,) = _calls(tree, "start_default_model_prewarm")
    (start,) = _calls(tree, "start_scheduled_refresh")
    assert start > prewarm
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call) and n.lineno == start)
    assert {k.arg: ast.unparse(k.value) for k in call.keywords} == {
        "origin": "server.BASE", "thread_factory": "owned_runtime_thread",
        "stop": "cloud_refresh_stop"}


def test_cmd_mcp_starts_the_schedule_after_membership_in_both_branches():
    tree = ast.parse((REPO / "sonder_runtime" / "__main__.py").read_text(encoding="utf-8"))
    cmd_mcp = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "cmd_mcp")
    membership, starts = _calls(cmd_mcp, "start_inference_membership"), _calls(cmd_mcp, "start_scheduled_refresh")
    assert len(membership) == len(starts) == 2
    merged = sorted([(line, "m") for line in membership] + [(line, "s") for line in starts])
    assert [kind for _line, kind in merged] == ["m", "s", "m", "s"]
    for call in (n for n in ast.walk(cmd_mcp) if isinstance(n, ast.Call) and n.lineno in starts):
        assert [ast.unparse(k.value) for k in call.keywords] == ["config.ollama.url"]
