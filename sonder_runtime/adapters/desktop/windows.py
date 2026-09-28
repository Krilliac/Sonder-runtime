"""Win32 desktop access for gated computer use: windows, capture, input.

Every public function runs with per-monitor DPI awareness on the calling
thread, so window rectangles, captures and cursor positions share one physical
pixel space. Nothing here decides policy; the caller has already checked the
allowlist, the session and the permission gate, and passes an HWND it vetted.
The one check this module owns is physical: before any pointer action it
confirms the point really lands on the vetted window and nothing covers it.
"""
from __future__ import annotations

import contextlib
import ctypes
import sys
from dataclasses import dataclass

from .png import encode_bgra

IS_WINDOWS = sys.platform == "win32"


class DesktopUnavailable(RuntimeError):
    """This host cannot provide the desktop operation."""


class TargetMoved(RuntimeError):
    """The vetted window is gone, hidden, or covered at the requested point."""


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    title: str
    app: str
    pid: int
    left: int
    top: int
    width: int
    height: int
    minimized: bool


@dataclass(frozen=True)
class Capture:
    hwnd: int
    image_width: int
    image_height: int
    client_width: int
    client_height: int
    png: bytes


_api = None


def _load():
    global _api
    if _api is not None:
        return _api
    if not IS_WINDOWS:
        raise DesktopUnavailable("computer use needs a Windows desktop session")
    from ctypes import wintypes as w

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    dwmapi = ctypes.WinDLL("dwmapi")

    def sig(fn, res, *args):
        fn.restype, fn.argtypes = res, list(args)

    HWND, HDC, HBITMAP = w.HWND, w.HDC, w.HBITMAP
    ENUMPROC = ctypes.WINFUNCTYPE(w.BOOL, HWND, w.LPARAM)
    sig(user32.EnumWindows, w.BOOL, ENUMPROC, w.LPARAM)
    sig(user32.IsWindow, w.BOOL, HWND)
    sig(user32.IsWindowVisible, w.BOOL, HWND)
    sig(user32.IsIconic, w.BOOL, HWND)
    sig(user32.GetWindowTextLengthW, ctypes.c_int, HWND)
    sig(user32.GetWindowTextW, ctypes.c_int, HWND, w.LPWSTR, ctypes.c_int)
    sig(user32.GetWindowThreadProcessId, w.DWORD, HWND, ctypes.POINTER(w.DWORD))
    sig(user32.GetClientRect, w.BOOL, HWND, ctypes.POINTER(w.RECT))
    sig(user32.ClientToScreen, w.BOOL, HWND, ctypes.POINTER(w.POINT))
    sig(user32.GetForegroundWindow, HWND)
    sig(user32.SetForegroundWindow, w.BOOL, HWND)
    sig(user32.AttachThreadInput, w.BOOL, w.DWORD, w.DWORD, w.BOOL)
    sig(user32.ShowWindow, w.BOOL, HWND, ctypes.c_int)
    sig(user32.BringWindowToTop, w.BOOL, HWND)
    sig(user32.WindowFromPoint, HWND, w.POINT)
    sig(user32.GetAncestor, HWND, HWND, w.UINT)
    sig(user32.GetWindow, HWND, HWND, w.UINT)
    sig(user32.GetDC, HDC, HWND)
    sig(user32.ReleaseDC, ctypes.c_int, HWND, HDC)
    sig(user32.PrintWindow, w.BOOL, HWND, HDC, w.UINT)
    sig(user32.SetCursorPos, w.BOOL, ctypes.c_int, ctypes.c_int)
    sig(user32.SendInput, w.UINT, w.UINT, ctypes.c_void_p, ctypes.c_int)
    sig(user32.GetLastInputInfo, w.BOOL, ctypes.c_void_p)
    sig(user32.MapVirtualKeyW, w.UINT, w.UINT, w.UINT)
    try:
        sig(user32.SetThreadDpiAwarenessContext, ctypes.c_void_p, ctypes.c_void_p)
    except AttributeError:
        pass
    sig(gdi32.CreateCompatibleDC, HDC, HDC)
    sig(gdi32.CreateCompatibleBitmap, HBITMAP, HDC, ctypes.c_int, ctypes.c_int)
    sig(gdi32.SelectObject, w.HGDIOBJ, HDC, w.HGDIOBJ)
    sig(gdi32.DeleteObject, w.BOOL, w.HGDIOBJ)
    sig(gdi32.DeleteDC, w.BOOL, HDC)
    sig(gdi32.SetStretchBltMode, ctypes.c_int, HDC, ctypes.c_int)
    sig(gdi32.SetBrushOrgEx, w.BOOL, HDC, ctypes.c_int, ctypes.c_int, ctypes.c_void_p)
    sig(gdi32.StretchBlt, w.BOOL, HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.DWORD)
    sig(gdi32.GetDIBits, ctypes.c_int, HDC, HBITMAP, w.UINT, w.UINT, ctypes.c_void_p,
        ctypes.c_void_p, w.UINT)
    sig(kernel32.OpenProcess, w.HANDLE, w.DWORD, w.BOOL, w.DWORD)
    sig(kernel32.CloseHandle, w.BOOL, w.HANDLE)
    sig(kernel32.QueryFullProcessImageNameW, w.BOOL, w.HANDLE, w.DWORD, w.LPWSTR,
        ctypes.POINTER(w.DWORD))
    sig(kernel32.GetTickCount, w.DWORD)
    sig(kernel32.GetCurrentThreadId, w.DWORD)
    sig(dwmapi.DwmGetWindowAttribute, ctypes.c_long, HWND, w.DWORD, ctypes.c_void_p, w.DWORD)

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", w.LONG), ("dy", w.LONG), ("mouseData", w.DWORD),
                    ("dwFlags", w.DWORD), ("time", w.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", w.WORD), ("wScan", w.WORD), ("dwFlags", w.DWORD),
                    ("time", w.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", w.DWORD), ("wParamL", w.WORD), ("wParamH", w.WORD)]

    class _U(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", w.DWORD), ("u", _U)]

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", w.UINT), ("dwTime", w.DWORD)]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", w.DWORD), ("biWidth", w.LONG), ("biHeight", w.LONG),
                    ("biPlanes", w.WORD), ("biBitCount", w.WORD), ("biCompression", w.DWORD),
                    ("biSizeImage", w.DWORD), ("biXPelsPerMeter", w.LONG),
                    ("biYPelsPerMeter", w.LONG), ("biClrUsed", w.DWORD),
                    ("biClrImportant", w.DWORD)]

    class _Api:
        pass

    api = _Api()
    api.w, api.user32, api.gdi32, api.kernel32, api.dwmapi = w, user32, gdi32, kernel32, dwmapi
    api.ENUMPROC, api.INPUT, api.MOUSEINPUT, api.KEYBDINPUT = ENUMPROC, INPUT, MOUSEINPUT, KEYBDINPUT
    api.LASTINPUTINFO, api.BITMAPINFOHEADER = LASTINPUTINFO, BITMAPINFOHEADER
    _api = api
    return api


# DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
_PER_MONITOR_V2 = ctypes.c_void_p(-4)


@contextlib.contextmanager
def _physical_pixels():
    api = _load()
    setter = getattr(api.user32, "SetThreadDpiAwarenessContext", None)
    previous = setter(_PER_MONITOR_V2) if setter else None
    try:
        yield api
    finally:
        if setter and previous:
            setter(previous)


def _app_name(api, pid: int) -> str:
    handle = api.kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        size = api.w.DWORD(1024)
        buf = ctypes.create_unicode_buffer(1024)
        if not api.kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return ""
        return buf.value.replace("\\", "/").rsplit("/", 1)[-1].lower()
    finally:
        api.kernel32.CloseHandle(handle)


def _info(api, hwnd) -> WindowInfo | None:
    if not api.user32.IsWindow(hwnd) or not api.user32.IsWindowVisible(hwnd):
        return None
    cloaked = api.w.DWORD(0)
    api.dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), 4)  # DWMWA_CLOAKED
    if cloaked.value:
        return None
    length = api.user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return None
    buf = ctypes.create_unicode_buffer(min(length, 511) + 1)
    api.user32.GetWindowTextW(hwnd, buf, len(buf))
    pid = api.w.DWORD(0)
    api.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    rect = api.w.RECT()
    api.user32.GetClientRect(hwnd, ctypes.byref(rect))
    origin = api.w.POINT(0, 0)
    api.user32.ClientToScreen(hwnd, ctypes.byref(origin))
    return WindowInfo(
        hwnd=int(hwnd), title=buf.value, app=_app_name(api, pid.value), pid=int(pid.value),
        left=origin.x, top=origin.y, width=rect.right - rect.left,
        height=rect.bottom - rect.top, minimized=bool(api.user32.IsIconic(hwnd)),
    )


def list_windows() -> list[WindowInfo]:
    """Visible, titled, uncloaked top-level windows, front to back."""
    with _physical_pixels() as api:
        found: list[WindowInfo] = []

        def visit(hwnd, _lparam):
            info = _info(api, hwnd)
            if info is not None:
                found.append(info)
            return len(found) < 512

        callback = api.ENUMPROC(visit)  # held so it outlives the enumeration
        api.user32.EnumWindows(callback, 0)
        return found


def window(hwnd: int) -> WindowInfo:
    with _physical_pixels() as api:
        info = _info(api, int(hwnd))
        if info is None:
            raise TargetMoved("the window is closed or hidden")
        return info


def foreground() -> int:
    with _physical_pixels() as api:
        return int(api.user32.GetForegroundWindow() or 0)


def focus(hwnd: int) -> None:
    """Bring the vetted window to the front, or raise ``TargetMoved``."""
    with _physical_pixels() as api:
        handle = int(hwnd)
        if not api.user32.IsWindow(handle):
            raise TargetMoved("the window is closed")
        if api.user32.IsIconic(handle):
            api.user32.ShowWindow(handle, 9)  # SW_RESTORE
        current = int(api.user32.GetForegroundWindow() or 0)
        if current == hwnd:
            return
        # Join the foreground window's input queue for the switch instead of
        # injecting input: an Alt tap would land in whatever window is in front
        # (maybe a non-allowlisted one) and can open its menu. No input is sent.
        mine = api.kernel32.GetCurrentThreadId()
        theirs = api.user32.GetWindowThreadProcessId(current, None) if current else 0
        attached = bool(theirs and theirs != mine and api.user32.AttachThreadInput(mine, theirs, True))
        try:
            api.user32.BringWindowToTop(handle)
            api.user32.SetForegroundWindow(handle)
        finally:
            if attached:
                api.user32.AttachThreadInput(mine, theirs, False)
        if int(api.user32.GetForegroundWindow() or 0) != hwnd:
            raise TargetMoved("Windows refused to bring the window to the front")


def capture(hwnd: int, *, max_width: int = 1280) -> Capture:
    """The window's client area as PNG, scaled to at most ``max_width``.

    ``PrintWindow`` renders the window itself, so overlapping windows (and the
    driving indicator) never appear in the capture.
    """
    with _physical_pixels() as api:
        handle = int(hwnd)
        rect = api.w.RECT()
        if not api.user32.GetClientRect(handle, ctypes.byref(rect)):
            raise TargetMoved("the window is closed")
        cw, ch = rect.right - rect.left, rect.bottom - rect.top
        if cw <= 0 or ch <= 0:
            raise TargetMoved("the window has no visible client area (minimized?)")
        scale = min(1.0, max_width / cw)
        iw, ih = max(1, int(cw * scale)), max(1, int(ch * scale))
        screen = api.user32.GetDC(None)
        full_dc = api.gdi32.CreateCompatibleDC(screen)
        small_dc = api.gdi32.CreateCompatibleDC(screen)
        full_bmp = api.gdi32.CreateCompatibleBitmap(screen, cw, ch)
        small_bmp = api.gdi32.CreateCompatibleBitmap(screen, iw, ih)
        old_full = api.gdi32.SelectObject(full_dc, full_bmp)
        old_small = api.gdi32.SelectObject(small_dc, small_bmp)
        try:
            # PW_CLIENTONLY | PW_RENDERFULLCONTENT
            if not api.user32.PrintWindow(handle, full_dc, 1 | 2):
                raise TargetMoved("the window could not be rendered")
            api.gdi32.SetStretchBltMode(small_dc, 4)  # HALFTONE
            api.gdi32.SetBrushOrgEx(small_dc, 0, 0, None)
            api.gdi32.StretchBlt(small_dc, 0, 0, iw, ih, full_dc, 0, 0, cw, ch, 0x00CC0020)
            header = api.BITMAPINFOHEADER()
            header.biSize = ctypes.sizeof(header)
            header.biWidth, header.biHeight = iw, -ih  # negative: top-down rows
            header.biPlanes, header.biBitCount = 1, 32
            buffer = ctypes.create_string_buffer(iw * ih * 4)
            api.gdi32.SelectObject(small_dc, old_small)
            rows = api.gdi32.GetDIBits(small_dc, small_bmp, 0, ih, buffer, ctypes.byref(header), 0)
            if rows != ih:
                raise TargetMoved("the capture could not be read back")
            data = encode_bgra(iw, ih, buffer.raw)
        finally:
            api.gdi32.SelectObject(full_dc, old_full)
            api.gdi32.DeleteObject(full_bmp)
            api.gdi32.DeleteObject(small_bmp)
            api.gdi32.DeleteDC(full_dc)
            api.gdi32.DeleteDC(small_dc)
            api.user32.ReleaseDC(None, screen)
        return Capture(hwnd, iw, ih, cw, ch, data)


def _screen_point(api, hwnd: int, x: int, y: int):
    """The screen point for client (x, y), refused unless it lands on ``hwnd``."""
    handle = int(hwnd)
    point = api.w.POINT(int(x), int(y))
    if not api.user32.ClientToScreen(handle, ctypes.byref(point)):
        raise TargetMoved("the window is closed")
    hit = api.user32.WindowFromPoint(point)
    root = api.user32.GetAncestor(hit, 2) if hit else None  # GA_ROOT
    if int(root or 0) != hwnd:
        raise TargetMoved("another window covers that point")
    return point


def _send(api, inputs) -> None:
    array = (api.INPUT * len(inputs))(*inputs)
    sent = api.user32.SendInput(len(inputs), ctypes.byref(array), ctypes.sizeof(api.INPUT))
    if sent != len(inputs):
        raise TargetMoved("Windows blocked the input (the window may run elevated)")


def _mouse(api, flags: int, data: int = 0):
    item = api.INPUT(type=0)
    item.u.mi = api.MOUSEINPUT(0, 0, ctypes.c_uint32(data).value, flags, 0, 0)
    return item


_EXTENDED = frozenset({0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E})


def _key(api, vk: int, up: bool):
    item = api.INPUT(type=1)
    flags = (0x2 if up else 0) | (0x1 if vk in _EXTENDED else 0)
    scan = api.user32.MapVirtualKeyW(vk, 0)
    item.u.ki = api.KEYBDINPUT(vk, scan, flags, 0, 0)
    return item


def _send_keys(api, strokes) -> None:
    _send(api, [_key(api, vk, up) for vk, up in strokes])


_VK = {
    "enter": 0x0D, "tab": 0x09, "escape": 0x1B, "backspace": 0x08, "delete": 0x2E,
    "insert": 0x2D, "space": 0x20, "home": 0x24, "end": 0x23, "pageup": 0x21,
    "pagedown": 0x22, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "ctrl": 0x11, "alt": 0x12, "shift": 0x10,
    **{f"f{n}": 0x6F + n for n in range(1, 13)},
}


def _vk(name: str) -> int:
    if name in _VK:
        return _VK[name]
    if len(name) == 1 and name.isalnum():
        return ord(name.upper())
    raise ValueError(f"unsupported key {name!r}")


def pointer(hwnd: int, action: str, x: int, y: int, *, notches: int = 0) -> None:
    """Move to client (x, y) of the vetted window and click, or scroll there."""
    with _physical_pixels() as api:
        point = _screen_point(api, hwnd, x, y)
        api.user32.SetCursorPos(point.x, point.y)
        if action == "move":
            return
        if action == "scroll":
            _send(api, [_mouse(api, 0x0800, 120 * int(notches))])  # WHEEL
            return
        down, up = (0x0008, 0x0010) if action == "right_click" else (0x0002, 0x0004)
        clicks = 2 if action == "double_click" else 1
        _send(api, [_mouse(api, flag) for _ in range(clicks) for flag in (down, up)])


def chord(keys: tuple[str, ...]) -> None:
    """Press a validated chord: modifiers down, key, modifiers up."""
    with _physical_pixels() as api:
        codes = [_vk(k) for k in keys]
        strokes = [(c, False) for c in codes] + [(c, True) for c in reversed(codes)]
        _send_keys(api, strokes)


def type_text(text: str) -> None:
    """Type text as Unicode key events; newline and tab become Enter and Tab."""
    with _physical_pixels() as api:
        items = []
        for ch in text:
            if ch == "\n":
                items += [_key(api, 0x0D, False), _key(api, 0x0D, True)]
                continue
            if ch == "\t":
                items += [_key(api, 0x09, False), _key(api, 0x09, True)]
                continue
            units = ch.encode("utf-16-le")
            for i in range(0, len(units), 2):
                unit = int.from_bytes(units[i:i + 2], "little")
                for up in (False, True):
                    item = api.INPUT(type=1)
                    item.u.ki = api.KEYBDINPUT(0, unit, 0x4 | (0x2 if up else 0), 0, 0)
                    items.append(item)
        for start in range(0, len(items), 200):
            _send(api, items[start:start + 200])


def idle_ticks() -> tuple[int, int]:
    """``(now, last_input)`` in milliseconds from the same tick counter."""
    with _physical_pixels() as api:
        info = api.LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        api.user32.GetLastInputInfo(ctypes.byref(info))
        return int(api.kernel32.GetTickCount()), int(info.dwTime)
