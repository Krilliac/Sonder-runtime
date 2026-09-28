"""Conservative natural-language recognition for gated desktop control.

This module only recognizes a desktop-control *intent*. It never decides that
an action is safe and never bypasses the permission policy owned by the tool
dispatcher.
"""
from __future__ import annotations

from dataclasses import dataclass
import re


COMPUTER_TOOLS = frozenset({
    "computer_use_status", "window_list", "computer_use_start",
    "computer_use_stop", "screen_capture", "ui_action", "computer_task",
})

_POLITE_RE = re.compile(r"^(?:(?:can|could|would|will) you )?(?:please )?", re.I)
_STOP_RE = re.compile(
    r"(?:stop (?:controlling|driving)(?: (?:my )?(?:computer|desktop))?|"
    r"stop computer use)", re.I,
)
_SCREEN_RE = re.compile(
    r"(?:what(?:'s| is) on my screen|show me my screen|look at my screen|read my screen)", re.I,
)
_CODING_RE = re.compile(
    r"\b(?:python|code|coding|function|handler|file|repo(?:sitory)?|"
    r"parser|type\s+hints?|source\s+file|pull\s+request|commit|"
    r"screenshot|documentation|docs?)\b", re.I,
)
_APPS = (r"(?:notepad|paint|calculator|calc|explorer|browser|chrome|edge|"
         r"microsoft word|excel|terminal|powershell|cmd|outlook|discord|slack)(?:\.exe)?(?= |$)")
_EXPLICIT_RE = re.compile(
    r"^(?:(?:use|control|drive) (?:my |the )?(?:computer|desktop)\b|"
    r"(?:control|drive|open|launch|start) (?:the )?" + _APPS + r")", re.I,
)
_APP_ACTION_RE = re.compile(
    r"^(?:click|double[- ]click|right[- ]click|type|press|write|enter|scroll)\b"
    r".+\b(?:in|into|on|using) (?:the )?" + _APPS, re.I,
)


@dataclass(frozen=True)
class ComputerUseIntent:
    tool: str
    args: dict[str, object]


def classify(text: str) -> ComputerUseIntent | None:
    """Recognize an explicit desktop request, leaving ordinary coding to chat/work."""
    goal = str(text or "").strip()
    value = re.sub(r"\s+", " ", goal.replace("\u2019", "'")).strip().rstrip(".!?")
    if not value or len(value) > 4000:
        return None
    value = _POLITE_RE.sub("", value, count=1)
    if _STOP_RE.fullmatch(value):
        return ComputerUseIntent("computer_use_stop", {})
    if _SCREEN_RE.fullmatch(value):
        return ComputerUseIntent("screen_capture", {"question": goal})
    if _EXPLICIT_RE.search(value) or (
        not _CODING_RE.search(value) and _APP_ACTION_RE.search(value)
    ):
        return ComputerUseIntent("computer_task", {"goal": goal, "max_steps": 10})
    return None
