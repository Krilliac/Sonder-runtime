"""Pure rules for semantic (UI Automation) perception of the driven window.

Nothing here touches the desktop. The adapter (``adapters/desktop/uia.py``)
reads a window's control tree into ``RawControl`` records; this module turns
them into a compact, bounded control table with stable refs, decides whether a
ref still names the control it named when the table was read, picks how to
act on a control (a UI Automation pattern before synthetic input), and reads
the state change an action caused.

Control names and values come from the screen, so they are untrusted data.
The table is always handed to callers inside the untrusted-observation
envelope. The content of documents, web views and edit fields is withheld:
their values are never shown, static text inside them is not listed, and the
actionable controls inside them (links, buttons, fields) are listed with
clipped names marked untrusted, so page text reaches a planner only as the
short accessible name of something it could act on.
Like the vision reading, a control's name can only *add* a confirmation.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

from .rules import NORMALIZED_SCALE, clean_text

MAX_ROWS = 200
# Controls the walker may visit to build one table (visible or not).
MAX_NODES = 1500
MAX_NAME_CHARS = 80
MAX_UNTRUSTED_NAME_CHARS = 60
MAX_VALUE_CHARS = 80

# UIA_*ControlTypeId (UIAutomationClient.h), 50000..50040.
CONTROL_TYPES = {
    50000: "Button", 50001: "Calendar", 50002: "CheckBox", 50003: "ComboBox",
    50004: "Edit", 50005: "Hyperlink", 50006: "Image", 50007: "ListItem",
    50008: "List", 50009: "Menu", 50010: "MenuBar", 50011: "MenuItem",
    50012: "ProgressBar", 50013: "RadioButton", 50014: "ScrollBar", 50015: "Slider",
    50016: "Spinner", 50017: "StatusBar", 50018: "Tab", 50019: "TabItem",
    50020: "Text", 50021: "ToolBar", 50022: "ToolTip", 50023: "Tree",
    50024: "TreeItem", 50025: "Custom", 50026: "Group", 50027: "Thumb",
    50028: "DataGrid", 50029: "DataItem", 50030: "Document", 50031: "SplitButton",
    50032: "Window", 50033: "Pane", 50034: "Header", 50035: "HeaderItem",
    50036: "Table", 50037: "TitleBar", 50038: "Separator", 50039: "SemanticZoom",
    50040: "AppBar",
}
PATTERNS = frozenset({"invoke", "toggle", "value", "selection_item", "expand_collapse"})
_INTERACTIVE = frozenset({
    "Button", "CheckBox", "ComboBox", "Edit", "Hyperlink", "ListItem", "MenuItem",
    "RadioButton", "Slider", "Spinner", "TabItem", "TreeItem", "SplitButton",
    "DataItem", "HeaderItem", "Document",
})
# Panes whose text is content, not interface: what they show is withheld.
_CONTENT_ROLES = frozenset({"Document", "Edit"})
# Hosts of web content. Everything below one is page content.
_WEB_VIEW_CLASSES = frozenset({
    "chrome_renderwidgethosthwnd", "internet explorer_server", "mozillawindowclass",
    "webview2", "microsoft.ui.xaml.controls.webview2",
})
_TOGGLE = {0: "unchecked", 1: "checked", 2: "indeterminate"}
_EXPAND = {0: "collapsed", 1: "expanded", 2: "partly expanded"}
REF_PATTERN = re.compile(r"c[0-9a-f]{6,12}")


@dataclass(frozen=True)
class RawControl:
    """One control as the adapter read it; rect is screen physical pixels."""

    runtime_id: tuple[int, ...]
    control_type: int
    name: str = ""
    value: str | None = None
    class_name: str = ""
    enabled: bool = True
    offscreen: bool = False
    password: bool = False
    rect: tuple[int, int, int, int] = (0, 0, 0, 0)  # left, top, width, height
    patterns: frozenset = frozenset()
    toggle_state: int | None = None
    selected: bool | None = None
    expanded: int | None = None
    value_read_only: bool | None = None
    depth: int = 0
    # The walker's parent (index into the same list), -1 for the window itself.
    parent: int = -1
    # The adapter's live element; opaque here and never compared.
    handle: object = field(default=None, compare=False, repr=False)

    @property
    def role(self) -> str:
        return CONTROL_TYPES.get(int(self.control_type), "Control")


@dataclass(frozen=True)
class RefEntry:
    """What a ref named when the table was read: its identity and fingerprint."""

    runtime_id: tuple[int, ...]
    role: str
    name: str
    # Inside (or is) a document, web view or edit pane: its name is page text.
    untrusted: bool = False

    @property
    def fingerprint(self) -> tuple[str, str]:
        return (self.role, self.name)


@dataclass(frozen=True)
class ControlTable:
    rows: tuple[dict, ...]
    refs: dict
    seen: int
    truncated: bool

    def render(self) -> str:
        return render_rows(self.rows)


def make_ref(runtime_id, length: int = 6) -> str:
    digest = hashlib.sha1(",".join(str(int(p)) for p in runtime_id).encode()).hexdigest()
    return "c" + digest[:length]


def fingerprint(raw: RawControl) -> tuple[str, str]:
    return (raw.role, clean_text(raw.name))


def _untrusted_flags(raws) -> list[bool]:
    """Which controls sit in (or are) a document, web view or edit pane."""
    flags = []
    for raw in raws:
        inherited = 0 <= raw.parent < len(flags) and flags[raw.parent]
        own = raw.role in _CONTENT_ROLES or raw.class_name.strip().lower() in _WEB_VIEW_CLASSES
        flags.append(bool(inherited or own))
    return flags


def _clip(rect, window) -> tuple[int, int, int, int] | None:
    """``rect`` clipped to the window client, in client pixels; None if outside."""
    left, top, width, height = (int(v) for v in rect)
    wl, wt, ww, wh = (int(v) for v in window)
    x0, y0 = max(left, wl), max(top, wt)
    x1, y1 = min(left + width, wl + ww), min(top + height, wt + wh)
    if width <= 0 or height <= 0 or x1 <= x0 or y1 <= y0:
        return None
    return (x0 - wl, y0 - wt, x1 - x0, y1 - y0)


def client_point(raw: RawControl, window) -> tuple[int, int] | None:
    """The centre of the control's visible part, in window client pixels."""
    clipped = _clip(raw.rect, window)
    if clipped is None:
        return None
    x, y, w, h = clipped
    return (x + w // 2, y + h // 2)


def _state(raw: RawControl) -> list[str]:
    state = [] if raw.enabled else ["disabled"]
    if raw.toggle_state in _TOGGLE:
        state.append(_TOGGLE[raw.toggle_state])
    if raw.selected:
        state.append("selected")
    if raw.expanded in _EXPAND:
        state.append(_EXPAND[raw.expanded])
    return state


def _actionable(raw: RawControl) -> bool:
    return bool(raw.patterns & PATTERNS) or raw.role in _INTERACTIVE


def _worth_a_row(raw: RawControl, in_content: bool) -> bool:
    if in_content and raw.role not in _CONTENT_ROLES:
        # Inside a document, web view or edit pane a named control that cannot
        # be acted on is page text (every text run is a Text control whose name
        # is the text): it is not listed. Actionable controls (links, buttons,
        # fields) are, with their names clipped and marked untrusted.
        return _actionable(raw)
    return _actionable(raw) or bool(clean_text(raw.name))


def build_table(raws, *, window, max_rows: int = MAX_ROWS) -> ControlTable:
    """The visible, on-screen controls of one window as a bounded table.

    ``window`` is the client rectangle ``(left, top, width, height)`` in the
    same screen pixels as the controls. Each row's ``ref`` is derived from the
    control's UI Automation runtime id, so it stays the same across reads for
    as long as the control exists.
    """
    raws = list(raws)
    untrusted = _untrusted_flags(raws)
    wl, wt, ww, wh = (int(v) for v in window)
    rows: list[dict] = []
    refs: dict[str, RefEntry] = {}
    visible = 0
    for raw, hidden_text in zip(raws, untrusted, strict=True):
        if raw.parent < 0 or raw.offscreen or not raw.runtime_id:
            continue
        clipped = _clip(raw.rect, window)
        if clipped is None or not _worth_a_row(raw, hidden_text):
            continue
        visible += 1
        if len(rows) >= max_rows:
            continue
        ref = make_ref(raw.runtime_id)
        if ref in refs and refs[ref].runtime_id != tuple(raw.runtime_id):
            ref = make_ref(raw.runtime_id, 12)
        if ref in refs:
            continue
        name_limit = MAX_UNTRUSTED_NAME_CHARS if hidden_text and raw.role not in _CONTENT_ROLES \
            else MAX_NAME_CHARS
        name = clean_text(raw.name)
        row = {"ref": ref, "role": raw.role, "name": name[:name_limit]}
        if raw.value is not None:
            if hidden_text or raw.password:
                row["value"] = None  # withheld: document, web or edit content
            else:
                row["value"] = clean_text(raw.value, MAX_VALUE_CHARS)
        state = _state(raw)
        if state:
            row["state"] = state
        x, y, w, h = clipped
        row["rect"] = [x, y, w, h]
        row["at"] = [round((x + w / 2) * NORMALIZED_SCALE / max(1, ww)),
                     round((y + h / 2) * NORMALIZED_SCALE / max(1, wh))]
        if hidden_text:
            row["untrusted"] = True
        rows.append(row)
        refs[ref] = RefEntry(tuple(raw.runtime_id), raw.role, name, bool(hidden_text))
    return ControlTable(tuple(rows), refs, visible, visible > len(rows))


def render_rows(rows) -> str:
    """One compact line per control, for a model prompt or a caller."""
    lines = []
    for row in rows:
        parts = [row["ref"], row["role"], json.dumps(row["name"], ensure_ascii=False)]
        if "value" in row:
            parts.append("value=withheld" if row["value"] is None
                         else "value=" + json.dumps(row["value"], ensure_ascii=False))
        parts.extend(row.get("state", ()))
        if row.get("untrusted"):
            parts.append("content-untrusted")
        parts.append("at=%d,%d" % tuple(row["at"]))
        lines.append(" ".join(parts))
    return "\n".join(lines)


def find(raws, runtime_id) -> RawControl | None:
    wanted = tuple(runtime_id)
    for raw in raws:
        if tuple(raw.runtime_id) == wanted:
            return raw
    return None


REOBSERVE = "re-observe with screen_capture(controls=true) and use a fresh ref"


def check_target(entry: RefEntry, raw: RawControl | None, window) -> str:
    """Why the ref may not be acted on now, or ``""``."""
    if raw is None:
        return "the control is gone (stale ref); " + REOBSERVE
    if fingerprint(raw) != entry.fingerprint:
        # Names are untrusted screen text: say what changed, never echo the text.
        what = "role" if raw.role != entry.role else "name"
        return "the control changed since it was read (its %s differs); %s" % (what, REOBSERVE)
    if not raw.enabled:
        return "the control is disabled"
    if raw.offscreen or client_point(raw, window) is None:
        return "the control is not visible in the window; " + REOBSERVE
    return ""


def topmost(runtime_id, hit_chain) -> bool:
    """Whether the element hit at the control's point is the control or inside it."""
    wanted = tuple(runtime_id)
    return any(tuple(rid) == wanted for rid in hit_chain)


# Methods, strongest first. Patterns act on the control itself, not a point.
PATTERN_METHODS = frozenset({"invoke", "toggle", "select", "set_value"})
_SET_VALUE_ROLES = frozenset({"Edit", "ComboBox", "Spinner"})


def choose_method(action: str, raw: RawControl, text: str = "") -> str:
    """How to perform ``action`` on ``raw``: a UIA pattern when one fits."""
    patterns = raw.patterns
    if action == "click":
        if "invoke" in patterns:
            return "invoke"
        if "toggle" in patterns:
            return "toggle"
        if "selection_item" in patterns:
            return "select"
        return "pointer"
    if action == "type":
        # SetValue replaces a field's text; typing inserts at the caret. They
        # agree only on an empty field, so SetValue is used only there and a
        # typed ref never erases what a field (or a classic Notepad buffer) held.
        if ("value" in patterns and raw.value_read_only is False and raw.value == ""
                and raw.role in _SET_VALUE_ROLES and "\n" not in text and "\t" not in text):
            return "set_value"
        return "focus_type"
    if action == "key":
        return "focus_key"
    return "pointer"


_COMPARED = ("name", "value", "enabled", "toggle_state", "selected", "expanded", "rect")


def verify(method: str, before: RawControl, after: RawControl | None, *, text: str = "") -> dict:
    """What changed on the control, and whether the change the method implies happened.

    Values are compared here but never reported: they may be withheld content.
    """
    if after is None:
        return {"method": method, "element": "gone", "changed": [],
                "expected": "the control's state to change", "expected_met": None,
                "note": "the control no longer exists (a dialog may have closed)"}
    changed = [f for f in _COMPARED if getattr(before, f) != getattr(after, f)]
    expected, met = "no specific state change (the result is not observable here)", None
    if method == "toggle":
        expected, met = "toggle state to change", after.toggle_state != before.toggle_state
    elif method == "select":
        expected, met = "the item to be selected", after.selected is True
    elif method == "set_value":
        expected = "the control's value to equal the typed text"
        met = None if after.value is None else after.value == text
    elif method == "focus_type":
        expected = "the control's value to change"
        met = None if after.value is None or before.value is None else after.value != before.value
    return {"method": method, "element": "present", "changed": changed,
            "expected": expected, "expected_met": met}


def parse_ref(text) -> str:
    """The ``ref`` a model's JSON step names, or ``""``."""
    match = re.search(r"\{.*\}", str(text or ""), re.DOTALL)
    if not match:
        return ""
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return ""
    ref = str(data.get("ref") or "").strip().lower() if isinstance(data, dict) else ""
    return ref if REF_PATTERN.fullmatch(ref) else ""


__all__ = [
    "CONTROL_TYPES", "ControlTable", "MAX_NODES", "MAX_ROWS", "PATTERN_METHODS", "PATTERNS",
    "REOBSERVE", "RawControl", "RefEntry", "build_table", "check_target", "choose_method",
    "client_point", "find", "fingerprint", "make_ref", "parse_ref", "render_rows", "topmost",
    "verify",
]
