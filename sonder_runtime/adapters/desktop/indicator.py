"""The visible "Sonder is driving" bar and its kill switch, as its own process.

Run by the session controller with ``python -m``. It shows a small topmost bar
that never takes focus, registers a global hotkey, and on the hotkey, the Stop
button, or its parent's exit it writes the stop file and quits. The controller
refuses every action while this process is not running, so the indicator
cannot silently disappear while Sonder keeps driving.

Exit codes: 0 stopped normally, 3 the hotkey is taken (the session must not
start without a working kill switch), 4 no desktop/Tk available.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import threading
import time
from pathlib import Path

HOTKEY_TEXT = "Ctrl+Alt+Shift+K"
_MOD_ALT, _MOD_CONTROL, _MOD_SHIFT, _MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x4000
_VK_K = 0x4B
_WM_HOTKEY = 0x0312
_WM_QUIT = 0x0012
_HOTKEY_ID = 0x5D0E


def _write(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, path)


def _parent_alive(kernel32, handle) -> bool:
    return kernel32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT


def _load_win32():
    """Return (user32, kernel32) with the prototypes this module calls."""
    from ctypes import wintypes as w

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.RegisterHotKey.argtypes = [w.HWND, ctypes.c_int, w.UINT, w.UINT]
    user32.UnregisterHotKey.argtypes = [w.HWND, ctypes.c_int]
    user32.PeekMessageW.argtypes = [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT, w.UINT]
    user32.GetMessageW.restype = w.BOOL
    user32.GetMessageW.argtypes = [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT]
    user32.PostThreadMessageW.argtypes = [w.DWORD, w.UINT, w.WPARAM, w.LPARAM]
    user32.GetParent.restype = w.HWND
    user32.GetParent.argtypes = [w.HWND]
    user32.GetWindowLongW.argtypes = [w.HWND, ctypes.c_int]
    user32.SetWindowLongW.argtypes = [w.HWND, ctypes.c_int, ctypes.c_long]
    kernel32.OpenProcess.restype = w.HANDLE
    kernel32.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel32.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
    kernel32.GetCurrentThreadId.restype = w.DWORD
    return user32, kernel32


class _HotkeyListener:
    """Owns the kill hotkey on a dedicated thread that runs its own message loop.

    RegisterHotKey(NULL, ...) posts WM_HOTKEY to the queue of the thread that
    registered it. Tk's notifier on the main thread removes and dispatches
    every message in that thread's queue, so a hotkey registered there is
    dropped before a filtered PeekMessage can see it. Here the registering
    thread is one that nothing else reads, and it unregisters the hotkey
    itself (UnregisterHotKey only works from the registering thread).

    ``fired`` is set when the hotkey is pressed. If the loop ends for any
    other reason the thread exits, ``is_alive()`` turns False and the caller
    must treat the kill switch as gone.
    """

    def __init__(self, user32, kernel32, modifiers: int, vk: int,
                 hotkey_id: int = _HOTKEY_ID) -> None:
        self._user32, self._kernel32 = user32, kernel32
        self._modifiers, self._vk, self._id = modifiers, vk, hotkey_id
        self.fired = threading.Event()
        self._started = threading.Event()
        self.registered = False
        self.thread_id = 0
        self._thread = threading.Thread(target=self._run, name="sonder-kill-hotkey", daemon=True)

    def start(self, timeout: float = 5.0) -> bool:
        """Start the thread; True once the hotkey is registered on it."""
        self._thread.start()
        return self._started.wait(timeout) and self.registered

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def stop(self, timeout: float = 2.0) -> None:
        if self.thread_id and self._thread.is_alive():
            self._user32.PostThreadMessageW(self.thread_id, _WM_QUIT, 0, 0)
            self._thread.join(timeout)

    def _run(self) -> None:
        from ctypes import wintypes as w

        msg = w.MSG()
        user32 = self._user32
        try:
            user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)  # PM_NOREMOVE: create the queue
            self.thread_id = int(self._kernel32.GetCurrentThreadId())
            self.registered = bool(user32.RegisterHotKey(None, self._id, self._modifiers, self._vk))
        finally:
            self._started.set()
        if not self.registered:
            return
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) not in (0, -1):
                if msg.message == _WM_HOTKEY and msg.wParam == self._id:
                    self.fired.set()
        finally:
            user32.UnregisterHotKey(None, self._id)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stop-file", required=True)
    parser.add_argument("--ready-file", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--parent", type=int, required=True)
    args = parser.parse_args(argv)
    stop_file, ready_file = Path(args.stop_file), Path(args.ready_file)
    if sys.platform != "win32":
        _write(ready_file, {"ok": False, "error": "not a Windows desktop"})
        return 4
    user32, kernel32 = _load_win32()
    parent = kernel32.OpenProcess(0x00100000, False, args.parent)  # SYNCHRONIZE
    if not parent:
        _write(ready_file, {"ok": False, "error": "parent process is gone"})
        return 4
    hotkey = _HotkeyListener(user32, kernel32,
                             _MOD_CONTROL | _MOD_ALT | _MOD_SHIFT | _MOD_NOREPEAT, _VK_K)
    if not hotkey.start():
        hotkey.stop()
        _write(ready_file, {"ok": False,
                            "error": f"the kill hotkey {HOTKEY_TEXT} is taken by another program"})
        return 3
    try:
        import tkinter as tk
    except Exception as exc:  # pragma: no cover - Tk ships with CPython on Windows
        hotkey.stop()
        _write(ready_file, {"ok": False, "error": f"Tk unavailable: {exc}"})
        return 4

    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.configure(bg="#b00020")
    text = f"  Sonder is driving {args.label}  -  press {HOTKEY_TEXT} or Stop  "
    tk.Label(root, text=text, fg="white", bg="#b00020",
             font=("Segoe UI", 10, "bold")).pack(side="left", pady=3)
    reason = {"value": ""}

    def stop(why: str) -> None:
        if not reason["value"]:
            reason["value"] = why
            _write(stop_file, {"reason": why, "at": time.time()})
        root.destroy()

    tk.Button(root, text="Stop", command=lambda: stop("stop button"),
              bg="white", fg="#b00020", relief="flat",
              font=("Segoe UI", 9, "bold")).pack(side="left", padx=4, pady=2)
    root.update_idletasks()
    width = root.winfo_reqwidth()
    root.geometry(f"+{max(0, (root.winfo_screenwidth() - width) // 2)}+0")
    root.update()
    # Never take focus from the window being driven.
    frame = user32.GetParent(root.winfo_id()) or root.winfo_id()
    style = user32.GetWindowLongW(frame, -20)  # GWL_EXSTYLE
    user32.SetWindowLongW(frame, -20, style | 0x08000000 | 0x00000080)  # NOACTIVATE|TOOLWINDOW

    def poll() -> None:
        if hotkey.fired.is_set():
            stop("kill hotkey")
            return
        if not hotkey.is_alive():
            stop("the kill hotkey stopped working")
            return
        if stop_file.exists():
            stop("stopped by the runtime")
            return
        if not _parent_alive(kernel32, parent):
            stop("the runtime exited")
            return
        root.attributes("-topmost", True)
        root.after(50, poll)

    _write(ready_file, {"ok": True, "pid": os.getpid(), "hotkey": HOTKEY_TEXT})
    root.after(50, poll)
    try:
        root.mainloop()
    finally:
        hotkey.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
