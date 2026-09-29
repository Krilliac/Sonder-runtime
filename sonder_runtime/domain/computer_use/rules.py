"""Pure rules for gated desktop computer use.

Nothing here touches the desktop. These functions decide whether an action is
well formed, whether it may leave the allowlisted window, whether it looks
irreversible (and so needs a person to confirm it), and how a vision model's
coordinates map onto a captured window. Screen content reaches this module only
as labels and text; it is treated as untrusted evidence that can *add* a
confirmation but can never remove one.
"""
from __future__ import annotations

import json
import re
import time
from collections import deque
from dataclasses import dataclass

ACTIONS = frozenset({"click", "double_click", "right_click", "move", "type", "key", "scroll"})
POINTER_ACTIONS = frozenset({"click", "double_click", "right_click", "move", "scroll"})
COORDINATE_SYSTEMS = frozenset({"pixels", "normalized"})
# Qwen-VL family models answer in a 0..1000 grid regardless of image size.
NORMALIZED_SCALE = 1000
MAX_TYPE_CHARS = 2000
MAX_LABEL_CHARS = 200
MAX_SCROLL_NOTCHES = 20

MODIFIERS = ("ctrl", "alt", "shift")
_NAMED_KEYS = frozenset({
    "enter", "tab", "escape", "backspace", "delete", "insert", "space",
    "home", "end", "pageup", "pagedown", "up", "down", "left", "right",
} | {f"f{n}" for n in range(1, 13)})
_KEY_ALIASES = {"return": "enter", "esc": "escape", "del": "delete", "control": "ctrl",
                "pgup": "pageup", "pgdn": "pagedown"}
# Chords that leave the allowlisted window or reach the OS shell. The Windows
# key is refused outright, so these are the remaining escapes.
_ESCAPE_CHORDS = frozenset({
    ("alt", "tab"), ("alt", "shift", "tab"), ("ctrl", "escape"), ("alt", "escape"),
    ("alt", "ctrl", "delete"), ("ctrl", "shift", "escape"), ("alt", "space"),
})

# Words that, on the control being clicked, mean the click may not be undoable.
IRREVERSIBLE_WORDS = (
    "send", "delete", "remove", "erase", "wipe", "purchase", "buy", "order",
    "pay", "checkout", "check out", "submit", "post", "publish", "transfer",
    "confirm", "uninstall", "format", "discard", "empty", "unsubscribe",
    "reply", "forward", "share", "accept", "approve", "deploy", "merge",
    "overwrite", "replace", "reset", "sign", "donate", "withdraw", "cancel subscription",
)
_IRREVERSIBLE = re.compile(
    r"(?<![a-z])(" + "|".join(re.escape(w) for w in IRREVERSIBLE_WORDS) + r")(?![a-z])",
    re.IGNORECASE,
)


class ActionRefused(ValueError):
    """The action is malformed or would leave the allowlisted window."""


def clean_text(value, limit: int = MAX_LABEL_CHARS) -> str:
    """Strip control characters and bound a screen- or caller-supplied string."""
    text = "".join(ch if ch >= " " or ch == "\n" else " " for ch in str(value or ""))
    return text.strip()[:limit]


def normalize_app(name) -> str:
    """The executable file name, lower-case, without a directory."""
    text = str(name or "").replace("\\", "/").rsplit("/", 1)[-1].strip().lower()
    return text


def app_allowed(app, allowed_apps) -> bool:
    name = normalize_app(app)
    return bool(name) and name in {normalize_app(a) for a in allowed_apps}


def parse_chord(keys) -> tuple[str, ...]:
    """Validate one key chord such as ``ctrl+s`` or ``enter``.

    Returns modifiers (in canonical order) followed by the key. Refuses the
    Windows key and every chord that switches away from the driven window.
    """
    raw = str(keys or "").strip().lower().replace(" ", "")
    if not raw or len(raw) > 40:
        raise ActionRefused("keys must be one chord such as 'ctrl+s' or 'enter'")
    parts = [_KEY_ALIASES.get(p, p) for p in raw.split("+")]
    if any(p in {"win", "windows", "meta", "super", "cmd"} for p in parts):
        raise ActionRefused("the Windows key is not available to computer use")
    *mods, key = parts
    if len(set(mods)) != len(mods) or any(m not in MODIFIERS for m in mods):
        raise ActionRefused("modifiers must be distinct: ctrl, alt, shift")
    if not (key in _NAMED_KEYS or re.fullmatch(r"[a-z0-9]", key)):
        raise ActionRefused(f"unsupported key {key!r}")
    chord = tuple(m for m in MODIFIERS if m in mods) + (key,)
    if tuple(sorted(chord[:-1]) + [chord[-1]]) in {
        tuple(sorted(c[:-1]) + [c[-1]]) for c in _ESCAPE_CHORDS
    }:
        raise ActionRefused("that chord leaves the driven window")
    if chord == ("alt", "f4"):
        # Closing the window can discard work; it is a confirmation, not a key.
        return chord
    return chord


def irreversible_reason(action: str, *, labels=(), text: str = "", chord=(),
                        app: str = "", submit_on_enter_apps=()) -> str:
    """Why this action needs a person to confirm it, or ``""``.

    ``labels`` are every description of the target that is available: the
    caller's own label and, when click verification is on, the vision model's
    reading of the control under the pointer. Any one of them is enough.
    """
    app = normalize_app(app)
    submit_app = app in {normalize_app(a) for a in submit_on_enter_apps}
    if action in {"click", "double_click"}:
        for label in labels:
            match = _IRREVERSIBLE.search(str(label or ""))
            if match:
                return f"the control reads as '{match.group(1).lower()}'"
    if action == "key":
        chord = tuple(chord)
        if chord == ("shift", "delete"):
            return "shift+delete deletes permanently"
        if chord == ("alt", "f4"):
            return "alt+f4 closes the window and may discard work"
        if chord[-1:] == ("delete",) and app == "explorer.exe":
            return "delete in File Explorer removes files"
        if chord[-1:] == ("enter",) and submit_app:
            return f"enter sends in {app}"
    if action == "type" and submit_app and "\n" in str(text or ""):
        return f"a newline sends in {app}"
    return ""


def to_pixels(x, y, coords: str, width: int, height: int) -> tuple[int, int]:
    """Map a caller or model coordinate onto the captured window's pixels."""
    if coords not in COORDINATE_SYSTEMS:
        raise ActionRefused("coords must be 'pixels' or 'normalized'")
    try:
        fx, fy = float(x), float(y)
    except (TypeError, ValueError):
        raise ActionRefused("x and y must be numbers") from None
    if coords == "normalized":
        if not (0 <= fx <= NORMALIZED_SCALE and 0 <= fy <= NORMALIZED_SCALE):
            raise ActionRefused("normalized coordinates must lie in 0..1000")
        fx = fx * width / NORMALIZED_SCALE
        fy = fy * height / NORMALIZED_SCALE
    px, py = min(int(round(fx)), width - 1), min(int(round(fy)), height - 1)
    if px < 0 or py < 0 or px >= width or py >= height:
        raise ActionRefused("the point is outside the driven window")
    return px, py


@dataclass(frozen=True)
class Action:
    action: str
    x: int | None = None
    y: int | None = None
    text: str = ""
    chord: tuple[str, ...] = ()
    scroll: int = 0
    label: str = ""


def build_action(action, *, x=None, y=None, coords="pixels", width=0, height=0,
                 text="", keys="", scroll=0, label="") -> Action:
    """Validate one requested action against the captured window size."""
    name = str(action or "").strip().lower()
    if name not in ACTIONS:
        raise ActionRefused("action must be one of: " + ", ".join(sorted(ACTIONS)))
    label = clean_text(label)
    if name in POINTER_ACTIONS:
        if x is None or y is None:
            raise ActionRefused(f"{name} needs x and y")
        px, py = to_pixels(x, y, coords, width, height)
    else:
        px = py = None
    if name in {"click", "double_click", "right_click"} and not label:
        raise ActionRefused("clicks need target_label: name the control you intend to press")
    body = ""
    chord: tuple[str, ...] = ()
    notches = 0
    if name == "type":
        body = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
        if not body or len(body) > MAX_TYPE_CHARS:
            raise ActionRefused(f"type needs 1..{MAX_TYPE_CHARS} characters of text")
        if any(ch < " " and ch not in "\n\t" for ch in body):
            raise ActionRefused("text may not contain control characters")
    elif name == "key":
        chord = parse_chord(keys)
    elif name == "scroll":
        try:
            notches = int(scroll)
        except (TypeError, ValueError):
            raise ActionRefused("scroll must be an integer") from None
        if notches == 0 or abs(notches) > MAX_SCROLL_NOTCHES:
            raise ActionRefused(f"scroll must be 1..{MAX_SCROLL_NOTCHES} notches, up (+) or down (-)")
    return Action(name, px, py, body, chord, notches, label)


class ActionBudget:
    """A sliding one-minute window plus a per-session total."""

    def __init__(self, per_minute: int, per_session: int, clock=time.monotonic):
        self._per_minute = int(per_minute)
        self._per_session = int(per_session)
        self._clock = clock
        self._recent: deque[float] = deque()
        self.used = 0

    def admit(self) -> str:
        """Spend one action; returns ``""`` or why it was refused."""
        now = self._clock()
        while self._recent and now - self._recent[0] >= 60.0:
            self._recent.popleft()
        if self.used >= self._per_session:
            return "the session's action budget is spent; start a new session"
        if len(self._recent) >= self._per_minute:
            return "more than %d actions in a minute; slow down" % self._per_minute
        self._recent.append(now)
        self.used += 1
        return ""


_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


def parse_model_step(text) -> dict:
    """The next step a vision model proposed, validated to a closed shape.

    Expected: ``{"done": bool, "action": str, "x": int, "y": int, "text": str,
    "keys": str, "scroll": int, "label": str, "reason": str}`` with x/y on the
    0..1000 grid. Unknown fields are dropped; the model's prose is clipped.
    """
    match = _JSON_OBJECT.search(str(text or ""))
    if not match:
        raise ActionRefused("the model did not return a JSON step")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        raise ActionRefused("the model returned malformed JSON") from None
    if not isinstance(data, dict):
        raise ActionRefused("the model's step must be a JSON object")
    step = {
        "done": data.get("done") is True,
        "action": clean_text(data.get("action"), 20).lower(),
        "x": data.get("x"),
        "y": data.get("y"),
        "text": str(data.get("text") or "")[:MAX_TYPE_CHARS],
        "keys": clean_text(data.get("keys"), 40),
        "scroll": data.get("scroll") or 0,
        "label": clean_text(data.get("label")),
        "reason": clean_text(data.get("reason"), 300),
    }
    return step


__all__ = [
    "ACTIONS", "Action", "ActionBudget", "ActionRefused", "IRREVERSIBLE_WORDS",
    "NORMALIZED_SCALE", "app_allowed", "build_action", "clean_text",
    "irreversible_reason", "normalize_app", "parse_chord", "parse_model_step",
    "to_pixels",
]
