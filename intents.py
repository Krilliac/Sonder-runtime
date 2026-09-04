"""intents — conservative natural-language classifier for sonder's control commands.

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

# Natural-language forms of the /consult, /route and /refactor commands, so the
# system reaches those capabilities whether the user types the slash or just
# asks for the thing. Each captures the payload the command needs.

# consult: "get a second opinion on X", "do the models agree about X",
# "ask another model whether X", "consult the models on X".
_CONSULT_RE = re.compile(
    r"^(?:can you\s+|please\s+|could you\s+)?"
    r"(?:get|give me|i want|i'd like)?\s*"
    r"(?:a\s+)?(?:second|another)\s+opinion\s+(?:on|about|for)\s+(?P<arg>.+)$",
    re.I,
)
_CONSULT_ALT_RE = re.compile(
    r"^(?:can you\s+|please\s+|could you\s+)?"
    r"(?:consult\s+(?:the\s+)?(?:models?|tiers?)|"
    r"ask\s+(?:another|a\s+second|other)\s+model|"
    r"do\s+the\s+models?\s+agree|"
    r"have\s+the\s+models?\s+weigh\s+in)"
    r"\s*(?:on|about|whether|that|if)?\s*(?P<arg>.+)$",
    re.I,
)

# route: "which tier/model should handle X", "which model is best for X",
# "what tier for X", "route this: X".
_ROUTE_RE = re.compile(
    r"^(?:which|what)\s+(?:tier|model)\s+"
    r"(?:should\s+(?:i\s+use\s+for|handle|do)|is\s+best\s+for|for|fits)\s+"
    r"(?P<arg>.+)$",
    re.I,
)
_ROUTE_ALT_RE = re.compile(
    r"^route\s+(?:this|it|the\s+request)?\s*[:\-]?\s*(?P<arg>.+)$",
    re.I,
)

# refactor one named function in a named .py file: "improve the parse function
# in foo.py", "refactor handle in a/b.py to drop the retry". Captures file,
# function and an optional trailing objective.
_REFACTOR_RE = re.compile(
    r"^(?:can you\s+|please\s+|could you\s+)?"
    r"(?:improve|refactor|clean\s*up|harden|tighten|fix)\s+"
    r"(?:the\s+)?(?:function\s+)?(?P<fn>[A-Za-z_]\w*)\s+"
    r"(?:function\s+)?in\s+(?P<file>\S+\.py)"
    r"(?:\s+(?:to|so\s+that|by|and)?\s*(?P<obj>.+))?$",
    re.I,
)


_WORK_QUESTION_RE = re.compile(
    r"^(how|what|why|who|when|where|which|explain|tell me about|show me how)\b"
)
_WORK_POLITE_RE = re.compile(
    r"^(please\s+|can you\s+|could you\s+|would you\s+|will you\s+)+"
)
_WORK_ACTION_RE = re.compile(
    r"\b(add|analyze|analyse|audit|benchmark|build|clean|compile|configure|continue|"
    r"create|debug|delete|deploy|design|develop|diagnose|"
    r"document|edit|execute|find|fix|format|"
    r"generate|implement|inspect|install|integrate|lint|list|make|merge|migrate|"
    r"modify|move|open|optimize|patch|profile|publish|read|refactor|"
    r"remove|rename|repair|restructure|review|rewrite|run|scan|scaffold|search|"
    r"setup|ship|start|stop|sync|test|troubleshoot|update|upgrade|validate|"
    r"verify|view|write)\b"
)
_WORK_TARGET_RE = re.compile(
    r"\b(animation|api|app|application|asset|audio|backend|binary|build|chart|"
    r"class|cli|code|component|config|controller|database|dashboard|data|"
    r"dependency|dependencies|diagram|directory|doc|docs|dockerfile|document|"
    r"endpoint|enum|extension|feature|file|files|fixture|folder|folders|form|"
    r"frontend|function|game|graphic|handler|hook|icon|image|interface|"
    r"library|logger|logo|makefile|manifest|method|middleware|migration|model|"
    r"module|music|package|parser|path|pipeline|plugin|presentation|program|"
    r"project|query|readme|renderer|report|repo|repository|route|scene|schema|"
    r"script|scripts|serializer|server|service|shader|sound|spec|spreadsheet|"
    r"sprite|struct|style|styles|system|table|task|template|test|tests|texture|"
    r"tool|tools|type|types|sonder|ui|validator|variable|vector|view|web|"
    r"webpage|website|workflow|workspace)\b"
)
_WORK_DIRECT_RE = re.compile(
    r"\b(use (the )?tools|work on|continue working|take care of|make the change|"
    r"implement it|fix it|edit it|run it|test it|build it|create it|debug it|"
    r"deploy it|refactor it|update it|ship it|clean it up|set it up|start it|"
    r"finish it|get it working|get it done|make it work)\b"
)
_PATH_LIKE_RE = re.compile(
    r"(?:[a-zA-Z]:[\\/]|[./~][\\/]|[\\/][\w.-]+|\.[a-zA-Z0-9]{1,8}\b)"
)
# Summarising or describing a *named file* needs the file, so it is workspace
# work even though the same verbs on a topic ("summarize this conversation")
# stay chat.  Measured 2026-09-03: "Summarize README.md in one sentence."
# reached the plain chat route and the model could only answer that it cannot
# read files.  The target must look like a file (a path, or a name with a
# letter extension of two or more characters, so "e.g." and "3.14" stay chat).
_WORK_FILE_READ_RE = re.compile(r"\b(summarize|summarise|describe|outline)\b")
_FILE_LIKE_RE = re.compile(
    r"(?:[a-zA-Z]:[\\/]|[./~][\\/]|[\\/][\w.-]+|\b[\w-]+\.[a-zA-Z]{2,8}\b)"
)

# This is deliberately narrower than a generic "security" or "sandbox"
# classifier.  Security reviews and tests remain legitimate workspace work.
# The host must, however, never turn a request to evade containment *and*
# contact something beyond the runtime into an autonomous tool run.  Such a
# request receives a deterministic refusal before a planner/model can decide
# which tools to try.
_CONTAINMENT_EVASION_RE = re.compile(
    r"\b(?:breach|escape|break\s+out\s+of|bypass|circumvent|evade)\s+"
    r"(?:the\s+)?(?:containment|sandbox|environment|isolation|guardrails?)\b",
    re.I,
)
_EXTERNAL_CONTACT_RE = re.compile(
    r"\b(?:email|sms|webhook)\b|\b(?:send|message|notify|contact|post|deliver)\b.*\b"
    r"(?:outside|external(?:ly)?|off[-\s]?host|another\s+(?:app|process)|"
    r"windows\s+notifications?|email|sms|webhook|slack|discord)\b",
    re.I | re.S,
)
_DEFENSIVE_CONTAINMENT_RE = re.compile(
    r"^\s*(?:fix|prevent|block|deny|refuse|harden)\b|^\s*add\s+(?:a\s+)?"
    r"(?:policy|guard|protection|test)\b", re.I,
)

CONTAINMENT_EGRESS_REFUSAL = (
    "Refused: autonomous runs cannot attempt containment evasion or send "
    "messages outside Sonder. I can help review the configured tool policy "
    "or add a safe, local containment test instead."
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

# This is a routing cue, not a general-purpose text classifier.  It accepts a
# complete, imperative user turn with a concrete task after an explicit
# delimiter.  In particular, it does not scan quoted/retrieved material for a
# fragment such as "compiler-feedback retries" and it does not turn a request
# to *explain* the workflow into execution.
_ENSEMBLE_COMPILER_RETRY_RE = re.compile(
    r"^\s*(?:(?:please\s+)?(?:use|run|start|enable)\s+)?(?:an?\s+)?"
    r"ensemble\s*(?:\(\s*)?code\s*(?:and|\+|plus)\s*reasoning\s*(?:\)\s*)?"
    r"with\s+compiler(?:[-\s]+)feedback\s+retries(?:\s+enabled)?\s+"
    r"(?:to|for|on)\s+(.+?)\s*[.!]?\s*$",
    re.IGNORECASE | re.DOTALL,
)


def requests_ensemble_compiler_retries(text):
    """Whether a whole user turn explicitly requests the bounded code loop.

    The returned task is deliberately not interpreted here: the host pins the
    model set, retry count, project root, permission mode, and executable
    policy later.  This recognizer simply prevents a loose phrase match from
    upgrading ordinary/retrieved prose into an execution request.
    """
    value = str(text or "")
    match = _ENSEMBLE_COMPILER_RETRY_RE.match(value)
    return bool(match and match.group(1).strip())


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


def classify_command(text):
    """Map a natural-language turn to one of the tier-aware commands, or None.

    Returns {"command": "consult"|"route"|"refactor", "arg": str}. This is what
    lets "get a second opinion on X" reach /consult, "which model should handle
    Y" reach /route and "improve foo in bar.py" reach the guarded /refactor,
    without the user having to remember the slash. Conservative on purpose: each
    pattern needs an explicit trigger phrase, so a plain coding question is never
    hijacked. The slash commands remain the exact, unambiguous form.
    """
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if not value or value.startswith("/"):
        return None

    # refactor is checked first: it needs a file AND a function, so a match is a
    # strong, specific signal that should win over the looser consult/route cues.
    m = _REFACTOR_RE.match(value)
    if m:
        obj = (m.group("obj") or "").strip()
        arg = "%s %s%s" % (m.group("file"), m.group("fn"), (" " + obj) if obj else "")
        return {"command": "refactor", "arg": arg}

    for pattern in (_CONSULT_RE, _CONSULT_ALT_RE):
        m = pattern.match(value)
        if m and m.group("arg").strip():
            return {"command": "consult", "arg": m.group("arg").strip()}

    for pattern in (_ROUTE_RE, _ROUTE_ALT_RE):
        m = pattern.match(value)
        if m and m.group("arg").strip():
            return {"command": "route", "arg": m.group("arg").strip()}

    return None


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
        return bool(_WORK_FILE_READ_RE.search(candidate) and _FILE_LIKE_RE.search(value))
    return bool(_WORK_TARGET_RE.search(candidate) or _PATH_LIKE_RE.search(value))


def containment_egress_refusal(text):
    """Return the fixed refusal for explicit containment-escape egress asks.

    This is a host routing fence, not a model safety judgment.  It covers the
    combined dangerous request only, so ordinary security audits and local
    notification implementation work are still eligible for their normal,
    separately-gated routes.
    """
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if not value:
        return None
    if _DEFENSIVE_CONTAINMENT_RE.search(value):
        return None
    if _CONTAINMENT_EVASION_RE.search(value) and _EXTERNAL_CONTACT_RE.search(value):
        return CONTAINMENT_EGRESS_REFUSAL
    return None


def classify_execution(text):
    """Choose a bounded execution lane for an eligible natural work request.

    ``decide`` is intentionally not an execution mode. It asks the local router
    model to choose only between foreground workbench and persistent Autopilot;
    host code still owns authorization, policies, and the final dispatch.
    """
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if not value or value.startswith("/") or _EXECUTION_NO_TOOLS_RE.search(value.lower()):
        return None
    if requests_ensemble_compiler_retries(value):
        return {
            "mode": "workbench",
            "reason": "explicit bounded local ensemble/compiler-retry request",
            "plan_only": False,
            "actions": ["ensemble_codegen_build_loop"],
        }
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
