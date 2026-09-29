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
import time
from pathlib import Path

HOTKEY_TEXT = "Ctrl+Alt+Shift+K"
_MOD_ALT, _MOD_CONTROL, _MOD_SHIFT, _MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x4000
_VK_K = 0x4B
_WM_HOTKEY = 0x0312
_HOTKEY_ID = 0x5D0E


def _write(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, path)


def _parent_alive(kernel32, handle) -> bool:
    return kernel32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT


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
    from ctypes import wintypes as w

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.RegisterHotKey.argtypes = [w.HWND, ctypes.c_int, w.UINT, w.UINT]
    user32.PeekMessageW.argtypes = [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT, w.UINT]
    user32.GetParent.restype = w.HWND
    user32.GetParent.argtypes = [w.HWND]
    user32.GetWindowLongW.argtypes = [w.HWND, ctypes.c_int]
    user32.SetWindowLongW.argtypes = [w.HWND, ctypes.c_int, ctypes.c_long]
    kernel32.OpenProcess.restype = w.HANDLE
    kernel32.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel32.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
    parent = kernel32.OpenProcess(0x00100000, False, args.parent)  # SYNCHRONIZE
    if not parent:
        _write(ready_file, {"ok": False, "error": "parent process is gone"})
        return 4
    if not user32.RegisterHotKey(None, _HOTKEY_ID,
                                 _MOD_CONTROL | _MOD_ALT | _MOD_SHIFT | _MOD_NOREPEAT, _VK_K):
        _write(ready_file, {"ok": False,
                            "error": f"the kill hotkey {HOTKEY_TEXT} is taken by another program"})
        return 3
    try:
        import tkinter as tk
    except Exception as exc:  # pragma: no cover - Tk ships with CPython on Windows
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
    msg = w.MSG()

    def poll() -> None:
        if user32.PeekMessageW(ctypes.byref(msg), None, _WM_HOTKEY, _WM_HOTKEY, 1):
            stop("kill hotkey")
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
    root.mainloop()
    user32.UnregisterHotKey(None, _HOTKEY_ID)
    return 0


if __name__ == "__main__":
    sys.exit(main())
