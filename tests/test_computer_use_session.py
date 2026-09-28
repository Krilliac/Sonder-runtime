"""The driving session re-proves its premise before every action (adapters/desktop/session.py)."""
from __future__ import annotations

import json

import pytest

from sonder_runtime.adapters.desktop.session import SessionController, SessionRefused, _tick_after
from sonder_runtime.adapters.desktop.windows import WindowInfo


class FakeDesktop:
    def __init__(self):
        self.windows = {7: WindowInfo(7, "Untitled - Notepad", "notepad.exe", 100, 0, 0, 800, 600, False)}
        self.now_tick = 1000
        self.last_input = 900

    def window(self, hwnd):
        if hwnd not in self.windows:
            raise RuntimeError("closed")
        return self.windows[hwnd]

    def idle_ticks(self):
        return self.now_tick, self.last_input


class FakeHelper:
    def __init__(self):
        self.code = None
        self.terminated = False

    def poll(self):
        return self.code

    def terminate(self):
        self.terminated = True
        self.code = 0

    def wait(self, timeout=None):
        return self.code


def _launcher(helpers, ok=True):
    def launch(stop_file, ready_file, label):
        helper = FakeHelper()
        helpers.append(helper)
        ready_file.write_text(json.dumps({"ok": ok, "error": "hotkey taken"}), encoding="utf-8")
        return helper
    return launch


@pytest.fixture()
def rig(tmp_path):
    desktop, helpers, now = FakeDesktop(), [], [1000.0]
    ctl = SessionController(tmp_path, desktop=desktop, launcher=_launcher(helpers),
                            clock=lambda: now[0])
    return ctl, desktop, helpers, now


def _start(ctl, apps=("notepad.exe",)):
    return ctl.start(7, allowed_apps=apps, ttl_seconds=60, per_minute=10, per_session=10)


def test_start_needs_an_allowlisted_app(rig):
    ctl, *_ = rig
    with pytest.raises(SessionRefused, match="allowed_apps"):
        _start(ctl, apps=("mspaint.exe",))
    assert ctl.active is None


def test_start_fails_closed_without_a_working_kill_switch(tmp_path):
    helpers = []
    ctl = SessionController(tmp_path, desktop=FakeDesktop(), launcher=_launcher(helpers, ok=False))
    with pytest.raises(SessionRefused, match="hotkey taken"):
        _start(ctl)
    assert ctl.active is None and helpers[0].terminated


def test_a_live_session_passes_and_only_one_runs(rig):
    ctl, *_ = rig
    session = _start(ctl)
    assert ctl.require_live(("notepad.exe",)) is session
    with pytest.raises(SessionRefused, match="already running"):
        _start(ctl)


@pytest.mark.parametrize("breaker, reason", [
    (lambda ctl, d, h, now: now.__setitem__(0, 2000.0), "expired"),
    (lambda ctl, d, h, now: ctl.active.stop_file.write_text('{"reason": "kill hotkey"}'), "kill hotkey"),
    (lambda ctl, d, h, now: setattr(h[0], "code", 1), "indicator closed"),
    (lambda ctl, d, h, now: d.windows.clear(), "window closed"),
    (lambda ctl, d, h, now: d.windows.__setitem__(
        7, WindowInfo(7, "x", "notepad.exe", 999, 0, 0, 1, 1, False)), "another process"),
    (lambda ctl, d, h, now: d.windows.__setitem__(
        7, WindowInfo(7, "x", "cmd.exe", 100, 0, 0, 1, 1, False)), "allowed_apps"),
    (lambda ctl, d, h, now: setattr(d, "last_input", 1500), "a person used"),
])
def test_any_broken_premise_ends_the_session_instead_of_acting(rig, breaker, reason):
    ctl, desktop, helpers, now = rig
    _start(ctl)
    breaker(ctl, desktop, helpers, now)
    with pytest.raises(SessionRefused, match=reason):
        ctl.require_live(("notepad.exe",))
    assert ctl.active is None
    assert helpers[0].terminated or helpers[0].code is not None
    # And it stays ended: the next action names why, and does not act.
    with pytest.raises(SessionRefused, match="last session ended"):
        ctl.require_live(("notepad.exe",))


def test_sonders_own_input_is_not_mistaken_for_a_person(rig):
    ctl, desktop, *_ = rig
    session = _start(ctl)
    desktop.now_tick, desktop.last_input = 5000, 5000
    ctl.note_input(session)
    desktop.last_input = 5100  # inside the grace window after Sonder's own input
    assert ctl.require_live(("notepad.exe",)) is session


def test_tick_comparison_survives_the_32_bit_wrap():
    assert _tick_after(5, 0xFFFFFFF0)
    assert not _tick_after(0xFFFFFFF0, 5)
    assert not _tick_after(10, 10)
