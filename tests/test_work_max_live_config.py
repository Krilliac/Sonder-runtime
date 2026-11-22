"""work_max_live: previously valid configs keep loading; bad explicit values are reported."""
from __future__ import annotations

import pytest

from sonder_runtime.interfaces.http import work_runs
from sonder_runtime.platform import config as sonder_config


def test_raising_only_work_max_running_still_loads():
    cfg = sonder_config.load_config(env={"SONDER_HTTP_WORK_MAX_RUNNING": "16"})
    assert cfg.server.work_max_running == 16
    assert cfg.server.work_max_live == 0


def test_explicit_live_below_running_is_reported():
    env = {"SONDER_HTTP_WORK_MAX_RUNNING": "16", "SONDER_HTTP_WORK_MAX_LIVE": "10"}
    with pytest.raises(sonder_config.ConfigError) as caught:
        sonder_config.load_config(env=env)
    assert "work_max_live" in str(caught.value)
    assert "got 10" in str(caught.value)


def test_explicit_live_within_bounds_loads():
    cfg = sonder_config.load_config(env={"SONDER_HTTP_WORK_MAX_LIVE": "40"})
    assert cfg.server.work_max_live == 40


def test_explicit_live_above_cap_is_reported():
    with pytest.raises(sonder_config.ConfigError):
        sonder_config.load_config(env={"SONDER_HTTP_WORK_MAX_LIVE": "257"})


@pytest.mark.parametrize(("running", "live", "expected"), [
    (2, 0, 8), (16, 0, 16), (2, 40, 40), (16, 10, 16), (2, 999, 256),
])
def test_runner_resolves_live_bound(running, live, expected):
    runner = work_runs.WorkRunner(store=None, effects=None, thread_factory=None,
                                  max_running=running, max_live=live)
    assert runner.max_live == expected


@pytest.mark.parametrize("bad_live", [-1, True, False, "bad", None, 2.5])
def test_negative_and_bool_work_max_live_are_refused(bad_live):
    from dataclasses import replace

    cfg = sonder_config.SonderConfig(
        server=replace(sonder_config.ServerConfig(), work_max_live=bad_live),
    )
    errors: list[str] = []
    sonder_config._validate(cfg, errors)
    assert any("work_max_live" in err for err in errors)
