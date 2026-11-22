"""E5: run_project caps how many commands one call may run.

Every step gets the full timeout, so an uncapped command list turned one
bounded call into an unbounded run (N steps x MAX_TIMEOUT).
"""
import json
import sys

import pytest

import code_runner


def _commands(count):
    return json.dumps([[sys.executable, "-c", "pass"]] * count)


def test_run_project_refuses_too_many_commands(monkeypatch):
    launched = []
    monkeypatch.setattr(
        code_runner, "_run_process",
        lambda *args, **kwargs: launched.append(args) or {"ok": True},
    )
    with pytest.raises(ValueError, match="at most 16 commands"):
        code_runner.run_project(json.dumps({"main.py": "pass"}), _commands(17))
    assert launched == []


def test_run_project_accepts_commands_up_to_the_cap(monkeypatch):
    launched = []
    monkeypatch.setattr(
        code_runner, "_run_process",
        lambda *args, **kwargs: launched.append(args) or {"ok": True},
    )
    result = code_runner.run_project(
        json.dumps({"main.py": "pass"}), _commands(16),
    )
    assert result["ok"]
    assert len(launched) == 16
