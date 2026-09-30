"""UI Automation access to the vetted session window, over plain ctypes COM.

This reads a window's control tree (``walk``), hit-tests a screen point
(``hit_chain``) and performs UI Automation patterns (Invoke, Toggle,
SelectionItem, Value) and ``SetFocus`` on one control. It uses the system's
``UIAutomationCore`` through its COM vtables, so it needs no dependency beyond
the standard library (``comtypes``/``pywinauto`` are not runtime dependencies).

Like ``windows.py`` it decides no policy: the caller has proved the session,
checked the allowlist and passed the gates, and hands it a vetted HWND. It runs
with per-monitor DPI awareness, so rectangles share the physical pixel space of
``windows.py`` captures and pointer input.

Usage::

    with open_tree() as tree:
        controls = tree.walk(hwnd)          # list[RawControl], elements alive
        tree.invoke(controls[3])
"""
from __future__ import annotations

import contextlib
import ctypes
import time
import uuid

from ...domain.computer_use.controls import MAX_NODES, RawControl
from .windows import IS_WINDOWS, DesktopUnavailable, TargetMoved, _physical_pixels


class UiaError(TargetMoved):
    """A UI Automation call failed (the control or window went away, or refused)."""


def _guid(text: str):
    return (ctypes.c_byte * 16).from_buffer_copy(uuid.UUID(text).bytes_le)


CLSID_CUIAutomation = "ff48dba4-60ef-4201-aa87-54103eef594e"
IID_IUIAutomation = "30cbe57d-d9d0-452a-ab13-7ac5ac4825ee"
_PATTERN_IIDS = {
    # pattern id: (interface IID, vtable index of its action method)
    10000: ("fb377fbe-8ea6-46d5-9c73-6499642d3059", 3),  # Invoke.Invoke
    10015: ("94cf8058-9b8d-4ab9-8bfd-4cd0a33c8c70", 3),  # Toggle.Toggle
    10010: ("a8efa66a-0fda-421a-9194-38021f3578ea", 3),  # SelectionItem.Select
    10002: ("a94cd8b1-0844-4cd6-9d2d-640537ab39e9", 3),  # Value.SetValue(BSTR)
}
INVOKE, TOGGLE, SELECTION_ITEM, VALUE = 10000, 10015, 10010, 10002

# IUnknown::Release, then the vtable slots used here (UIAutomationClient.h).
_RELEASE = 2
_UIA_ELEMENT_FROM_HANDLE_BUILD_CACHE = 10
_UIA_ELEMENT_FROM_POINT = 7
_UIA_CONTROL_VIEW_WALKER = 14
_UIA_RAW_VIEW_WALKER = 16
_UIA_CREATE_CACHE_REQUEST = 20
_CACHE_ADD_PROPERTY = 3
_WALKER_GET_PARENT = 3
_WALKER_FIRST_CHILD_BUILD_CACHE = 10
_WALKER_NEXT_SIBLING_BUILD_CACHE = 12
_ELEMENT_SET_FOCUS = 3
_ELEMENT_CURRENT_PROPERTY = 10
_ELEMENT_CACHED_PROPERTY = 12
_ELEMENT_CURRENT_PATTERN_AS = 14

# UIA_*PropertyId
P_RUNTIME_ID, P_RECT, P_CONTROL_TYPE, P_NAME = 30000, 30001, 30003, 30005
P_ENABLED, P_CLASS, P_PASSWORD, P_OFFSCREEN = 30010, 30012, 30019, 30022
P_HAS_EXPAND, P_HAS_INVOKE, P_HAS_SELECTION_ITEM = 30028, 30031, 30036
P_HAS_TOGGLE, P_HAS_VALUE, P_VALUE, P_VALUE_READ_ONLY = 30041, 30043, 30045, 30046
P_EXPAND_STATE, P_SELECTED, P_TOGGLE_STATE = 30070, 30079, 30086
_CACHED = (P_RUNTIME_ID, P_RECT, P_CONTROL_TYPE, P_NAME, P_ENABLED, P_CLASS, P_PASSWORD,
           P_OFFSCREEN, P_HAS_EXPAND, P_HAS_INVOKE, P_HAS_SELECTION_ITEM, P_HAS_TOGGLE,
           P_HAS_VALUE, P_VALUE, P_VALUE_READ_ONLY, P_EXPAND_STATE, P_SELECTED, P_TOGGLE_STATE)
_PATTERN_FLAGS = ((P_HAS_INVOKE, "invoke"), (P_HAS_TOGGLE, "toggle"), (P_HAS_VALUE, "value"),
                  (P_HAS_SELECTION_ITEM, "selection_item"), (P_HAS_EXPAND, "expand_collapse"))

VT_I4, VT_R8, VT_BSTR, VT_BOOL, VT_ARRAY = 3, 5, 8, 11, 0x2000
_HR = ctypes.c_long
_SIZE = ctypes.sizeof(ctypes.c_void_p)


class _VARIANT(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort),
                ("r3", ctypes.c_ushort), ("data", ctypes.c_byte * (2 * _SIZE))]


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


_libs = None


def _load():
    global _libs
    if _libs is not None:
        return _libs
    if not IS_WINDOWS:
        raise DesktopUnavailable("UI Automation needs a Windows desktop session")
    ole32 = ctypes.WinDLL("ole32")
    oleaut32 = ctypes.WinDLL("oleaut32")
    ole32.CoInitializeEx.restype, ole32.CoInitializeEx.argtypes = _HR, [ctypes.c_void_p, ctypes.c_ulong]
    ole32.CoUninitialize.restype, ole32.CoUninitialize.argtypes = None, []
    ole32.CoCreateInstance.restype = _HR
    ole32.CoCreateInstance.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong,
                                       ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    oleaut32.VariantClear.restype, oleaut32.VariantClear.argtypes = _HR, [ctypes.c_void_p]
    oleaut32.SysAllocStringLen.restype = ctypes.c_void_p
    oleaut32.SysAllocStringLen.argtypes = [ctypes.c_wchar_p, ctypes.c_uint]
    oleaut32.SysFreeString.restype, oleaut32.SysFreeString.argtypes = None, [ctypes.c_void_p]
    oleaut32.SysStringLen.restype, oleaut32.SysStringLen.argtypes = ctypes.c_uint, [ctypes.c_void_p]
    for fn in ("SafeArrayGetLBound", "SafeArrayGetUBound"):
        getattr(oleaut32, fn).restype = _HR
        getattr(oleaut32, fn).argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_long)]
    oleaut32.SafeArrayAccessData.restype = _HR
    oleaut32.SafeArrayAccessData.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    oleaut32.SafeArrayUnaccessData.restype = _HR
    oleaut32.SafeArrayUnaccessData.argtypes = [ctypes.c_void_p]
    _libs = (ole32, oleaut32)
    return _libs


def _vcall(ptr, index, argtypes, *args, restype=_HR):
    vtable = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtable[index])(ptr, *args)


def _check(hr, what: str) -> None:
    if hr < 0:
        raise UiaError("UI Automation %s failed (0x%08X)" % (what, hr & 0xFFFFFFFF))


def _out(ptr, index, what, *args, argtypes=()) -> int:
    """Call a method whose last argument is an interface out-pointer."""
    result = ctypes.c_void_p()
    _check(_vcall(ptr, index, (*argtypes, ctypes.POINTER(ctypes.c_void_p)), *args,
                  ctypes.byref(result)), what)
    return int(result.value or 0)


def _release(ptr) -> None:
    if ptr:
        _vcall(ptr, _RELEASE, (), restype=ctypes.c_ulong)


def _variant_value(oleaut32, var: _VARIANT):
    vt = var.vt
    raw = ctypes.addressof(var) + 8
    if vt == VT_I4:
        return ctypes.c_long.from_address(raw).value
    if vt == VT_BOOL:
        return ctypes.c_short.from_address(raw).value != 0
    if vt == VT_BSTR:
        pointer = ctypes.c_void_p.from_address(raw).value
        if not pointer:
            return ""
        return ctypes.wstring_at(pointer, oleaut32.SysStringLen(pointer))
    if vt in (VT_ARRAY | VT_I4, VT_ARRAY | VT_R8):
        array = ctypes.c_void_p.from_address(raw).value
        if not array:
            return ()
        low, high = ctypes.c_long(), ctypes.c_long()
        oleaut32.SafeArrayGetLBound(array, 1, ctypes.byref(low))
        oleaut32.SafeArrayGetUBound(array, 1, ctypes.byref(high))
        count = max(0, high.value - low.value + 1)
        data = ctypes.c_void_p()
        if oleaut32.SafeArrayAccessData(array, ctypes.byref(data)) < 0:
            return ()
        try:
            kind = ctypes.c_long if vt == VT_ARRAY | VT_I4 else ctypes.c_double
            return tuple((kind * count).from_address(data.value)) if count else ()
        finally:
            oleaut32.SafeArrayUnaccessData(array)
    return None  # VT_EMPTY, or the "not supported" sentinel (VT_UNKNOWN)


class UiaTree:
    """One UI Automation client; every element it hands out lives until ``close``."""

    def __init__(self):
        self._ole32, self._oleaut32 = _load()
        self._held: list[int] = []
        hr = self._ole32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED
        # S_OK / S_FALSE need a matching uninitialize; RPC_E_CHANGED_MODE
        # means the thread already has an apartment we can use as it is.
        self._uninit = hr in (0, 1)
        if hr < 0 and (hr & 0xFFFFFFFF) != 0x80010106:
            _check(hr, "initialization")
        self._uia = 0
        try:
            client = ctypes.c_void_p()
            _check(self._ole32.CoCreateInstance(
                ctypes.byref(_guid(CLSID_CUIAutomation)), None, 1,  # CLSCTX_INPROC_SERVER
                ctypes.byref(_guid(IID_IUIAutomation)), ctypes.byref(client)), "client creation")
            self._uia = int(client.value or 0)
            self._cache = self._hold(_out(self._uia, _UIA_CREATE_CACHE_REQUEST, "cache request"))
            for prop in _CACHED:
                _check(_vcall(self._cache, _CACHE_ADD_PROPERTY, (ctypes.c_int,), prop),
                       "cache property")
        except BaseException:
            self.close()
            raise

    def _hold(self, ptr: int) -> int:
        if ptr:
            self._held.append(ptr)
        return ptr

    def _finish(self) -> None:
        if self._uninit:
            self._uninit = False
            self._ole32.CoUninitialize()

    def close(self) -> None:
        held, self._held = self._held, []
        for ptr in reversed(held):
            try:
                _release(ptr)
            except OSError:
                pass
        if self._uia:
            _release(self._uia)
            self._uia = 0
        self._finish()

    def _property(self, element: int, prop: int, *, cached: bool):
        var = _VARIANT()
        index = _ELEMENT_CACHED_PROPERTY if cached else _ELEMENT_CURRENT_PROPERTY
        hr = _vcall(element, index, (ctypes.c_int, ctypes.POINTER(_VARIANT)), prop, ctypes.byref(var))
        if hr < 0:
            return None
        try:
            return _variant_value(self._oleaut32, var)
        finally:
            self._oleaut32.VariantClear(ctypes.byref(var))

    def _record(self, element: int, depth: int, parent: int) -> RawControl:
        get = {prop: self._property(element, prop, cached=True) for prop in _CACHED}
        rect = get[P_RECT] or ()
        box = tuple(int(round(v)) for v in rect[:4]) if len(rect) >= 4 else (0, 0, 0, 0)
        patterns = frozenset(name for prop, name in _PATTERN_FLAGS if get[prop] is True)
        value = get[P_VALUE] if "value" in patterns and isinstance(get[P_VALUE], str) else None

        def as_int(v):
            return v if isinstance(v, int) and not isinstance(v, bool) else None

        return RawControl(
            runtime_id=tuple(int(p) for p in (get[P_RUNTIME_ID] or ())),
            control_type=as_int(get[P_CONTROL_TYPE]) or 0,
            name=get[P_NAME] if isinstance(get[P_NAME], str) else "",
            value=value,
            class_name=get[P_CLASS] if isinstance(get[P_CLASS], str) else "",
            enabled=get[P_ENABLED] is not False,
            offscreen=get[P_OFFSCREEN] is True,
            password=get[P_PASSWORD] is True,
            rect=box,
            patterns=patterns,
            toggle_state=as_int(get[P_TOGGLE_STATE]) if "toggle" in patterns else None,
            selected=(get[P_SELECTED] is True) if "selection_item" in patterns else None,
            expanded=as_int(get[P_EXPAND_STATE]) if "expand_collapse" in patterns else None,
            value_read_only=(get[P_VALUE_READ_ONLY] is True) if "value" in patterns else None,
            depth=depth, parent=parent, handle=element,
        )

    def walk(self, hwnd: int, *, max_nodes: int = MAX_NODES, max_seconds: float = 4.0,
             max_depth: int = 40) -> list[RawControl]:
        """The window's control-view tree, depth first, bounded in size and time."""
        with _physical_pixels():
            root = self._hold(_out(self._uia, _UIA_ELEMENT_FROM_HANDLE_BUILD_CACHE, "window lookup",
                                   ctypes.c_void_p(int(hwnd)), ctypes.c_void_p(self._cache),
                                   argtypes=(ctypes.c_void_p, ctypes.c_void_p)))
            if not root:
                raise UiaError("UI Automation cannot see that window")
            walker = self._hold(_out(self._uia, _UIA_CONTROL_VIEW_WALKER, "tree walker"))
            found = [self._record(root, 0, -1)]
            deadline = time.monotonic() + max_seconds
            stack = [(root, 0, 0)]  # element, its index in found, its depth
            while stack and len(found) < max_nodes and time.monotonic() < deadline:
                element, index, depth = stack.pop()
                if depth >= max_depth:
                    continue
                children = []
                child = self._hold(_out(walker, _WALKER_FIRST_CHILD_BUILD_CACHE, "child",
                                        ctypes.c_void_p(element), ctypes.c_void_p(self._cache),
                                        argtypes=(ctypes.c_void_p, ctypes.c_void_p)))
                while child and len(found) < max_nodes:
                    found.append(self._record(child, depth + 1, index))
                    children.append((child, len(found) - 1, depth + 1))
                    child = self._hold(_out(walker, _WALKER_NEXT_SIBLING_BUILD_CACHE, "sibling",
                                            ctypes.c_void_p(child), ctypes.c_void_p(self._cache),
                                            argtypes=(ctypes.c_void_p, ctypes.c_void_p)))
                stack.extend(reversed(children))
            return found

    def hit_chain(self, x: int, y: int, *, limit: int = 64) -> list[tuple[int, ...]]:
        """Runtime ids of the element at screen point (x, y) and its ancestors."""
        with _physical_pixels():
            walker = self._hold(_out(self._uia, _UIA_RAW_VIEW_WALKER, "raw walker"))
            element = self._hold(_out(self._uia, _UIA_ELEMENT_FROM_POINT, "hit test",
                                      _POINT(int(x), int(y)), argtypes=(_POINT,)))
            chain = []
            while element and len(chain) < limit:
                rid = self._property(element, P_RUNTIME_ID, cached=False)
                chain.append(tuple(int(p) for p in (rid or ())))
                element = self._hold(_out(walker, _WALKER_GET_PARENT, "parent",
                                          ctypes.c_void_p(element), argtypes=(ctypes.c_void_p,)))
            return chain

    def _pattern(self, control: RawControl, pattern: int) -> int:
        iid, _ = _PATTERN_IIDS[pattern]
        ptr = self._hold(_out(control.handle, _ELEMENT_CURRENT_PATTERN_AS, "pattern lookup",
                              ctypes.c_int(pattern), ctypes.byref(_guid(iid)),
                              argtypes=(ctypes.c_int, ctypes.c_void_p)))
        if not ptr:
            raise UiaError("the control no longer supports that pattern")
        return ptr

    def supports(self, control: RawControl, pattern: int) -> bool:
        """Whether the live control hands out ``pattern`` (a read; performs nothing)."""
        try:
            return bool(self._pattern(control, pattern))
        except UiaError:
            return False

    def _act(self, control: RawControl, pattern: int, what: str) -> None:
        ptr = self._pattern(control, pattern)
        _check(_vcall(ptr, _PATTERN_IIDS[pattern][1], ()), what)

    def invoke(self, control: RawControl) -> None:
        self._act(control, INVOKE, "Invoke")

    def toggle(self, control: RawControl) -> None:
        self._act(control, TOGGLE, "Toggle")

    def select(self, control: RawControl) -> None:
        self._act(control, SELECTION_ITEM, "Select")

    def set_value(self, control: RawControl, text: str) -> None:
        ptr = self._pattern(control, VALUE)
        bstr = self._oleaut32.SysAllocStringLen(text, len(text))
        try:
            _check(_vcall(ptr, _PATTERN_IIDS[VALUE][1], (ctypes.c_void_p,), bstr), "SetValue")
        finally:
            self._oleaut32.SysFreeString(bstr)

    def set_focus(self, control: RawControl) -> None:
        _check(_vcall(control.handle, _ELEMENT_SET_FOCUS, ()), "SetFocus")


@contextlib.contextmanager
def open_tree():
    """A UI Automation client for one operation; releases every element after."""
    tree = UiaTree()
    try:
        yield tree
    finally:
        tree.close()
