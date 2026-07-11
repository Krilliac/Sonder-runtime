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


_WORK_QUESTION_RE = re.compile(
    r"^(how|what|why|who|when|where|which|explain|tell me about|show me how)\b"
)
_WORK_POLITE_RE = re.compile(
    r"^(please\s+|can you\s+|could you\s+|would you\s+|will you\s+)+"
)
_WORK_ACTION_RE = re.compile(
    r"\b(add|audit|benchmark|build|compile|continue|create|delete|deploy|diagnose|"
    r"document|edit|execute|find|fix|"
    r"generate|implement|inspect|install|list|make|modify|move|open|read|refactor|"
    r"remove|rename|repair|review|run|scan|scaffold|search|ship|test|update|validate|"
    r"verify|view|write)\b"
)
_WORK_TARGET_RE = re.compile(
    r"\b(animation|api|app|application|asset|audio|background|brand|build|chart|"
    r"cli|code|config|dashboard|data|diagram|directory|doc|docs|document|file|"
    r"files|folder|folders|function|game|graphic|icon|image|library|logo|model|"
    r"music|package|path|presentation|program|project|readme|report|repo|repository|"
    r"scene|script|scripts|sound|spreadsheet|sprite|system|test|tests|texture|"
    r"tool|tools|trilobite|ui|vector|web|webpage|website|workspace)\b"
)
_WORK_DIRECT_RE = re.compile(
    r"\b(use (the )?tools|work on|continue working|take care of|make the change|implement it|fix it|"
    r"edit it|run it|test it|build it|create it)\b"
)
_PATH_LIKE_RE = re.compile(
    r"(?:[a-zA-Z]:[\\/]|[./~][\\/]|[\\/][\w.-]+|\.[a-zA-Z0-9]{1,8}\b)"
)

_EXECUTION_NO_TOOLS_RE = re.compile(
    r"\b(?:no tools?|do not use (?:any )?tools?|don't use (?:any )?tools?|"
    r"just answer|answer only|explain only)\b"
)
_EXECUTION_PLAN_ONLY_RE = re.compile(
    r"\b(?:plan only|planning only|make (?:me )?a plan(?: only)?|"
    r"plan (?:it|this) but (?:do not|don't) execute|do not execute(?: it)? yet)\b"
)
_EXECUTION_NO_BACKGROUND_RE = re.compile(
    r"\b(?:foreground|one[- ]shot|single pass|quick pass|"
    r"do not|don't)\s+(?:start|run|use)?\s*(?:it\s+)?(?:in\s+)?background\b|"
    r"\b(?:do it now|handle it inline|foreground only)\b"
)
_EXECUTION_FLEET_RE = re.compile(
    r"\b(?:fleet|swarm|fan[- ]?out|paral+el (?:sub)?agents?|paral+el workflow|"
    r"multiple subagents?|spawn (?:as many|as much|all|the maximum|maximum|max)?\s*"
    r"(?:sub)?agents?|"
    r"as many (?:sub)?agents? as (?:possible|the hardware allows))\b"
)
_EXECUTION_AUTOPILOT_RE = re.compile(
    r"\b(?:autonomously|autonomous(?:ly)?|autopilot|in the background|"
    r"keep working|continue working|do not stop|don't stop|continue until|work until|"
    r"end[- ]to[- ]end|from start to finish|take ownership|handle everything|"
    r"implement everything|finish (?:the )?(?:whole|entire)|plan and execute|"
    r"without (?:asking|waiting for) me)\b"
)
_EXECUTION_SEQUENCE_RE = re.compile(
    r"\b(?:then|after that|afterward|next|finally|and then|all the way through)\b"
)


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


def classify_work(text):
    """Return True for concrete workspace actions that should use real tools.

    This intentionally does not classify explanatory questions or pure content
    requests. A work request needs an action plus a workspace-like target, a
    path, or an explicit reference such as "fix it"/"use the tools".
    """
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if not value or value.startswith("/") or len(value) > 12000:
        return False
    lowered = value.lower()
    candidate = _WORK_POLITE_RE.sub("", lowered).strip()
    if _WORK_QUESTION_RE.match(candidate):
        return False
    if _WORK_DIRECT_RE.search(candidate):
        return True
    if not _WORK_ACTION_RE.search(candidate):
        return False
    return bool(_WORK_TARGET_RE.search(candidate) or _PATH_LIKE_RE.search(value))


def classify_execution(text):
    """Choose a bounded execution lane for an eligible natural work request.

    ``decide`` is intentionally not an execution mode. It asks the local router
    model to choose only between foreground workbench and persistent Autopilot;
    host code still owns authorization, policies, and the final dispatch.
    """
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if not value or value.startswith("/") or _EXECUTION_NO_TOOLS_RE.search(value.lower()):
        return None
    if not classify_work(value):
        return None
    lowered = value.lower()
    plan_only = bool(_EXECUTION_PLAN_ONLY_RE.search(lowered))
    actions = sorted(set(_WORK_ACTION_RE.findall(lowered)))
    if _EXECUTION_FLEET_RE.search(lowered):
        return {
            "mode": "fleet",
            "reason": "explicit fleet or parallel-agent request",
            "plan_only": False,
            "actions": actions,
        }
    if plan_only:
        return {
            "mode": "autopilot",
            "reason": "explicit persistent plan-only request",
            "plan_only": True,
            "actions": actions,
        }
    if _EXECUTION_NO_BACKGROUND_RE.search(lowered):
        return {
            "mode": "workbench",
            "reason": "explicit foreground or one-shot request",
            "plan_only": False,
            "actions": actions,
        }
    if _EXECUTION_AUTOPILOT_RE.search(lowered):
        return {
            "mode": "autopilot",
            "reason": "explicit autonomous or end-to-end request",
            "plan_only": False,
            "actions": actions,
        }
    compound = len(actions) >= 3 and (
        bool(_EXECUTION_SEQUENCE_RE.search(lowered))
        or len(value) >= 180
        or value.count(",") >= 2
    )
    if compound:
        return {
            "mode": "decide",
            "reason": "compound multi-stage work needs a bounded local mode decision",
            "plan_only": False,
            "actions": actions,
        }
    return {
        "mode": "workbench",
        "reason": "bounded foreground task",
        "plan_only": False,
        "actions": actions,
    }
