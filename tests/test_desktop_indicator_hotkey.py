"""The indicator's kill hotkey must reach its handler while Tk pumps messages.

RegisterHotKey(NULL, ...) posts WM_HOTKEY to the registering thread's queue.
Tk's notifier drains and dispatches every message on the main thread, so the
hotkey has to live on a thread whose queue nothing else reads.
"""
from __future__ import annotations

import ctypes
import json
import os
import sys
import threading
import time

import pytest

from sonder_runtime.adapters.desktop import indicator

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Win32 message queues")

# Ctrl+Alt+Shift+Win+F24: a combination nothing on a desktop uses, so the
# test never collides with the real kill hotkey of a running session.
_MODS = indicator._MOD_CONTROL | indicator._MOD_ALT | indicator._MOD_SHIFT | 0x8 | indicator._MOD_NOREPEAT
_VK_F24 = 0x87
_TEST_ID = 0x5D1F


def _drain_like_tk(user32) -> int:
    """Remove and dispatch every message on this thread, as Tk's notifier does."""
    from ctypes import wintypes as w

    msg = w.MSG()
    drained = 0
    while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):  # PM_REMOVE, no filter
        user32.DispatchMessageW(ctypes.byref(msg))
        drained += 1
    return drained


def _listener(user32, kernel32):
    return indicator._HotkeyListener(user32, kernel32, _MODS, _VK_F24, hotkey_id=_TEST_ID)


def test_hotkey_is_owned_by_a_thread_that_tk_does_not_pump():
    user32, kernel32 = indicator._load_win32()
    listener = _listener(user32, kernel32)
    try:
        assert listener.start(), "test hotkey could not be registered"
        assert listener.thread_id and listener.thread_id != kernel32.GetCurrentThreadId()
        _drain_like_tk(user32)
        # What the system does on a key press: WM_HOTKEY to the registering thread.
        assert user32.PostThreadMessageW(listener.thread_id, indicator._WM_HOTKEY, _TEST_ID, 0)
        deadline = time.monotonic() + 2.0
        while not listener.fired.is_set() and time.monotonic() < deadline:
            _drain_like_tk(user32)  # the main thread keeps pumping everything meanwhile
            time.sleep(0.01)
        assert listener.fired.is_set()
    finally:
        listener.stop()
    assert not listener.is_alive()


def test_taken_hotkey_fails_start_and_stop_releases_it_from_the_owning_thread():
    user32, kernel32 = indicator._load_win32()
    first = _listener(user32, kernel32)
    try:
        assert first.start()
        second = _listener(user32, kernel32)
        assert not second.start()  # already taken -> the session must not start
        second.stop()
        assert not second.is_alive()
    finally:
        first.stop()
    # UnregisterHotKey only succeeds on the registering thread; if it ran
    # anywhere else the combination would still be held here.
    third = _listener(user32, kernel32)
    try:
        assert third.start()
    finally:
        third.stop()


def test_other_thread_messages_do_not_fire_the_hotkey():
    user32, kernel32 = indicator._load_win32()
    listener = _listener(user32, kernel32)
    try:
        assert listener.start()
        assert user32.PostThreadMessageW(listener.thread_id, indicator._WM_HOTKEY, _TEST_ID + 1, 0)
        assert user32.PostThreadMessageW(listener.thread_id, 0x0400, _TEST_ID, 0)  # WM_USER
        time.sleep(0.2)
        assert not listener.fired.is_set()
        assert listener.is_alive()
    finally:
        listener.stop()


def test_listener_thread_ending_is_visible_so_the_indicator_fails_closed():
    """A broken message loop must not look like a working kill switch."""
    calls: list[tuple[str, int]] = []

    class _User32:
        def PeekMessageW(self, *a):
            return 0

        def RegisterHotKey(self, hwnd, ident, mods, vk):
            calls.append(("register", threading.get_ident()))
            return 1

        def GetMessageW(self, *a):
            return -1  # error from the message loop

        def UnregisterHotKey(self, hwnd, ident):
            calls.append(("unregister", threading.get_ident()))
            return 1

        def PostThreadMessageW(self, *a):
            return 1

    class _Kernel32:
        def GetCurrentThreadId(self):
            return 1234

    listener = indicator._HotkeyListener(_User32(), _Kernel32(), _MODS, _VK_F24, hotkey_id=_TEST_ID)
    assert listener.start()
    listener._thread.join(2.0)
    assert not listener.is_alive() and not listener.fired.is_set()
    (reg, reg_tid), (unreg, unreg_tid) = calls
    assert (reg, unreg) == ("register", "unregister")
    assert reg_tid == unreg_tid != threading.get_ident()


def test_main_exits_3_and_reports_when_the_hotkey_cannot_be_registered(tmp_path, monkeypatch):
    class _Taken:
        def __init__(self, *a, **k):
            self.stopped = False

        def start(self, timeout=5.0):
            return False

        def stop(self, timeout=2.0):
            self.stopped = True

    monkeypatch.setattr(indicator, "_HotkeyListener", _Taken)
    ready = tmp_path / "ready.json"
    code = indicator.main(["--stop-file", str(tmp_path / "stop.json"), "--ready-file", str(ready),
                           "--label", "test", "--parent", str(os.getpid())])
    assert code == 3
    payload = json.loads(ready.read_text(encoding="utf-8"))
    assert payload["ok"] is False and indicator.HOTKEY_TEXT in payload["error"]


def test_wm_hotkey_writes_stop_file_directly_without_tk_poll(tmp_path):
    user32, kernel32 = indicator._load_win32()
    stop_file = tmp_path / "stop.json"
    stop_lock = threading.Lock()
    listener = indicator._HotkeyListener(
        user32, kernel32, _MODS, _VK_F24, hotkey_id=_TEST_ID,
        stop_file=stop_file, stop_lock=stop_lock,
    )
    try:
        assert listener.start()
        assert not stop_file.exists()
        assert user32.PostThreadMessageW(listener.thread_id, indicator._WM_HOTKEY, _TEST_ID, 0)
        assert listener.fired.wait(2.0)
        # Written immediately on the hotkey thread before any Tk poll runs.
        assert stop_file.exists()
        data = json.loads(stop_file.read_text(encoding="utf-8"))
        assert data["reason"] == "kill hotkey"
    finally:
        listener.stop()


def test_unexpected_listener_exit_writes_stop_file_directly(tmp_path):
    stop_file = tmp_path / "stop.json"

    class _User32:
        def PeekMessageW(self, *a):
            return 0

        def RegisterHotKey(self, *a):
            return 1

        def GetMessageW(self, *a):
            return -1

        def UnregisterHotKey(self, *a):
            return 1

        def PostThreadMessageW(self, *a):
            return 1

    class _Kernel32:
        def GetCurrentThreadId(self):
            return 1234

    listener = indicator._HotkeyListener(
        _User32(), _Kernel32(), _MODS, _VK_F24, hotkey_id=_TEST_ID,
        stop_file=stop_file,
    )
    assert listener.start()
    listener._thread.join(2.0)
    assert stop_file.exists()
    data = json.loads(stop_file.read_text(encoding="utf-8"))
    assert data["reason"] == "the kill hotkey stopped working"
