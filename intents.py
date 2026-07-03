"""intents — conservative natural-language classifier for trilobite's control commands.

Lets short, control-like chat turns like "strict on, show your reasoning" or
"run it" work the same as their slash-command equivalents (/strict on, /trace
on, /run), without hijacking real coding questions or requests. Stdlib only.
"""
import re

# Messages that open with one of these are almost always a real question or
# task ("how do I...", "explain strict mode in javascript"), not a control
# command — even if they happen to contain a control-ish word later on. The
# one deliberate exception is "show me your reasoning", handled below.
_GUARD_RE = re.compile(r"^(how|what|why|explain|write|create|show me how)\b")

_SHOW_REASONING_RE = re.compile(r"show (me )?(your )?(reasoning|thinking)")

_TRACE_OFF_RE = re.compile(r"\b(trace|debug)\s+off\b")
_TRACE_ON_RE = re.compile(r"\b(trace|debug)\s+on\b")

_STRICT_OFF_RE = re.compile(r"\bstrict\s+off\b")
_STRICT_ON_RE = re.compile(r"\bstrict(\s+mode)?(\s+on)?\b")

_RUN_RE = re.compile(r"^(run|execute)\b.*\b(it|that|this|the code|code)\b")

_TRAIN_N_RE = re.compile(r"\btrain(\s+on)?\s+(\d+)")
_TRAIN_DEFAULT_RE = re.compile(
    r"\b(self.?train|train yourself|practice|improve yourself|learn something|teach yourself)\b"
)

TRAIN_DEFAULT_N = 3


def classify(text):
    """Return a dict of detected control intents, or {} for a normal task turn.

    Keys (any subset): 'trace': bool, 'strict': bool, 'run': True, 'train': int.
    Conservative: only fires on SHORT control-like messages (<= 10 words), and
    never fires on messages that read as a real question or task.
    """
    t = (text or "").strip().lower()
    if not t or len(t.split()) > 10:
        return {}

    is_show_reasoning = bool(_SHOW_REASONING_RE.search(t))
    if not is_show_reasoning and _GUARD_RE.match(t):
        return {}

    out = {}

    # trace / debug / show reasoning
    if _TRACE_OFF_RE.search(t):
        out["trace"] = False
    elif _TRACE_ON_RE.search(t) or is_show_reasoning:
        out["trace"] = True

    # strict
    if _STRICT_OFF_RE.search(t):
        out["strict"] = False
    elif _STRICT_ON_RE.search(t):
        out["strict"] = True

    # run it / execute
    if _RUN_RE.search(t) or t in ("run", "run it", "execute", "execute it"):
        out["run"] = True

    # self-train / practice / learn / improve
    m = _TRAIN_N_RE.search(t)
    if m:
        out["train"] = int(m.group(2))
    elif _TRAIN_DEFAULT_RE.search(t):
        out["train"] = TRAIN_DEFAULT_N

    return out
