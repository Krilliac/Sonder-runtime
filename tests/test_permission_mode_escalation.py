"""Unattended callers may lower the permission mode but never raise it (#24).

A legacy MCP client in ``manual`` could call ``permission_mode {mode: auto}``
(the tool is exempt from the gate so a client in ``plan`` can get out), then
run host programs; the mode persists in ``SONDER_HOME`` and so governed every
surface sharing that home. Raising autonomy above ``manual`` now needs an
attended caller -- the console ``/mode`` -- or the administrator-authorized
HTTP endpoint; lowering, and ``plan`` -> ``manual``, stay allowed everywhere.
"""
from __future__ import annotations

import asyncio
import io
import sys

import pytest

import permission_modes as pm
import server
import sonder_runtime.interfaces.repl.repl as sonder_repl


@pytest.fixture(autouse=True)
def mode_sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "_state_path", lambda: str(tmp_path / "mode.json"))
    saved = dict(pm._STATE)
    saved_loaded = pm._LOADED
    with pm._LOCK:
        pm._STATE.update(mode=pm.DEFAULT_MODE, elevated=False, elevation_reason="")
    pm._LOADED = True
    try:
        yield
    finally:
        with pm._LOCK:
            pm._STATE.update(saved)
        pm._LOADED = saved_loaded


class _FakeTty(io.StringIO):
    def isatty(self):
        return True


def _mcp(mode):
    result = asyncio.run(server.mcp.call_tool("permission_mode", {"mode": mode}))
    return "".join(getattr(item, "text", "") for item in result.content)


@pytest.mark.parametrize("start,target", [
    (pm.MANUAL, pm.AUTO),
    (pm.MANUAL, pm.ACCEPT_EDITS),
    (pm.ACCEPT_EDITS, pm.AUTO),
    (pm.PLAN, pm.AUTO),
    (pm.PLAN, pm.ACCEPT_EDITS),
])
def test_mcp_client_cannot_raise_autonomy(start, target):
    pm.set_mode(start)
    out = _mcp(target)
    assert pm.current_mode() == start
    assert out.startswith("refused")
    assert "/mode %s" % target in out
    assert "POST /v1/permission-mode" in out


@pytest.mark.parametrize("start,target", [
    (pm.AUTO, pm.MANUAL),
    (pm.AUTO, pm.PLAN),
    (pm.ACCEPT_EDITS, pm.MANUAL),
    (pm.MANUAL, pm.PLAN),
    (pm.PLAN, pm.MANUAL),
    (pm.AUTO, pm.AUTO),
])
def test_mcp_client_may_lower_the_mode_or_leave_plan(start, target):
    pm.set_mode(start)
    out = _mcp(target)
    assert pm.current_mode() == target
    assert not out.startswith("refused")


def test_any_direct_unattended_call_is_held_to_lowering():
    """control_command and the HTTP chat reach the tool function directly."""
    pm.set_mode(pm.MANUAL)
    out = server.permission_mode(mode="auto")
    assert out.startswith("refused")
    assert pm.current_mode() == pm.MANUAL


def test_the_console_is_attended_and_may_raise(monkeypatch):
    monkeypatch.setattr(sys, "stdin", _FakeTty())
    pm.set_mode(pm.MANUAL)
    assert "auto" in sonder_repl._mode_command("auto")
    assert pm.current_mode() == pm.AUTO


def test_attended_context_does_not_leak():
    with pm.attended_mode_change():
        assert pm.mode_change_attended()
    assert not pm.mode_change_attended()


def test_unknown_mode_is_still_a_plain_error():
    out = _mcp("nonsense")
    assert "unknown mode" in out
    assert pm.current_mode() == pm.MANUAL


def test_escalation_matrix():
    rank = {mode: i for i, mode in enumerate(pm.MODES)}
    for current in pm.MODES:
        for target in pm.MODES:
            expected = rank[target] > max(rank[current], rank[pm.MANUAL])
            assert pm.is_unattended_escalation(current, target) is expected
