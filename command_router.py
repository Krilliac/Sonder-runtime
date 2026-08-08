"""command_router -- resolve a natural-language turn to a slash command line.

The REPL already routes on slash commands. This lets the *same* commands be
reached by asking for the thing in plain language ("show me your stats" ->
``/stats``, "switch to the reasoning tier" -> ``/model reasoning``, "read file
foo.py" -> ``/read foo.py``). The resolver returns the synthesized slash line
and the REPL feeds it straight into its existing dispatch, so there is one
implementation per command and the slash form stays the exact, unambiguous way
to invoke it.

Design rules that keep this from hijacking real work:

* Every rule is anchored at the start of the turn and needs an explicit trigger
  phrase. A plain coding question ("how do I cache a parse result") or a task
  for the agent ("fix the failing API tests") matches nothing and falls through
  to the normal chat / workbench path.
* File and orchestration rules require an explicit keyword ("file <path>",
  "orchestrate ...") or a path-with-extension, so ordinary prose that merely
  mentions reading or fixing is left alone.
* The tier-aware trio (consult / route / refactor) is delegated to
  ``intents.classify_command`` so their argument extraction lives in one place.

Stdlib only.
"""
import re

import intents
import project_scaffold

_TIER_COMMANDS = {"consult", "route", "refactor"}


def _scaffold_action(match):
    """"create a new rust project named foo" -> /scaffold rust foo.

    Only fires when the whole line is the scaffold ask (the pattern is
    anchored to the end): a request that continues past the name ("...of the
    fibonacci sequence") is real implementation work and falls through to the
    agent, which owns the scaffold tool alongside its file tools.
    """
    kind = project_scaffold.normalize_kind(match.group("kind"))
    if not kind:
        return None
    name = (match.group("name") or "").strip() or "NewProject"
    return "/scaffold %s %s" % (kind, name)


def _fixed(slash):
    """A rule that always yields the same slash command, ignoring the match."""
    return lambda _m: slash


def _with_arg(slash):
    """A rule that appends the captured ``arg`` group; None when it is empty."""

    def build(match):
        arg = (match.group("arg") or "").strip()
        return ("%s %s" % (slash, arg)).strip() if arg else None

    return build


def _rule(pattern, action):
    return (re.compile(pattern, re.I), action)


# Ordered; first match wins. Specific patterns precede looser ones. Each pattern
# is anchored with ^ so it only fires when the turn OPENS with the trigger.
_RULES = [
    # --- lifecycle / session ---
    _rule(r"^(?:new session|start over|reset(?: the)? session|fresh session|"
          r"clear(?: the)? session|new thread)\b", _fixed("/new")),
    _rule(r"^(?:list|show|my)\s+sessions\b", _fixed("/sessions")),
    _rule(r"^resume(?:\s+session)?\s+(?P<arg>.+)$", _with_arg("/resume")),
    _rule(r"^(?:switch(?:\s+to)?|set|use|change(?:\s+to)?)\s+(?:the\s+)?project"
          r"(?:\s+to)?\s+(?P<arg>.+)$", _with_arg("/project")),
    _rule(r"^(?:current project|what(?:'s| is)\s+(?:the\s+)?(?:current\s+)?project)\b",
          _fixed("/project")),
    _rule(r"^(?:exit|quit|goodbye|bye|leave)\b", _fixed("/exit")),

    # --- identity / admin ---
    _rule(r"^who\s+am\s+i\b", _fixed("/whoami")),
    _rule(r"^(?:admin status|show admin)\b", _fixed("/admin")),
    _rule(r"^(?:list|show)?\s*accounts\b", _fixed("/accounts")),

    # --- memory / facts / lessons ---
    _rule(r"^(?:remember|note|memorize)(?:\s+(?:that|this))?\s+(?P<arg>.+)$",
          _with_arg("/fact")),
    _rule(r"^(?:show|list)?\s*(?:my\s+)?facts\b|^what\s+do\s+you\s+remember\b",
          _fixed("/facts")),
    _rule(r"^(?:show|list)?\s*(?:learned\s+)?lessons\b", _fixed("/lessons")),

    # --- status / info ---
    _rule(r"^(?:show\s+(?:me\s+)?(?:your\s+)?|what\s+are\s+(?:your\s+)?|runtime\s+|"
          r"usage\s+)?stats\b", _fixed("/stats")),
    _rule(r"^(?:show\s+)?context\s+health\b|^how'?s\s+the\s+context\b|"
          r"^context\s+usage\b", _fixed("/context")),
    _rule(r"^(?:show\s+)?(?:context\s+)?compaction(?:\s+plan)?\b|"
          r"^compact\s+(?:the\s+)?context\b", _fixed("/compact")),
    _rule(r"^(?:list\s+)?(?:all\s+)?commands\b|^command\s+registry\b|"
          r"^what\s+commands\b", _fixed("/commands")),
    _rule(r"^(?:show\s+)?permissions?\b|^permission\s+policy\b", _fixed("/permissions")),
    _rule(r"^(?:dump|save)\s+(?:the\s+)?(?:chat|debug)(?:\s+log|\s+dump)?\b"
          r"(?:\s+(?P<arg>\S+))?", lambda m: ("/dump %s" % (m.group("arg") or "")).strip()),

    # --- quality / privacy / emotion / prefs / improvement ---
    _rule(r"^(?:fix|repair)\s+(?:the\s+)?(?:memory\s+)?quality\b", _fixed("/qualityfix apply")),
    _rule(r"^(?:show\s+)?(?:memory\s+)?quality(?:\s+report)?\b", _fixed("/quality")),
    _rule(r"^(?:fix|repair)\s+privacy\b", _fixed("/privacyfix")),
    _rule(r"^(?:privacy\s+review|review\s+privacy)\b", _fixed("/privacyreview")),
    _rule(r"^(?:backfill|build)\s+(?:the\s+)?embeddings\b", _fixed("/embedfix")),
    _rule(r"^(?:show\s+)?(?:emotions?|mood|emotion\s+vectors)\b", _fixed("/emotion")),
    _rule(r"^(?:show\s+)?(?:my\s+)?preferences?\b(?:\s+(?P<arg>.+))?",
          lambda m: ("/prefer %s" % (m.group("arg") or "")).strip()),
    _rule(r"^(?:show\s+)?(?:system\s+)?improvements?(?:\s+report)?\b|"
          r"^what\s+should\s+(?:you|the\s+system)\s+improve\b", _fixed("/improve")),

    # --- agents / orchestration ---
    _rule(r"^(?:show\s+)?agents?\s+status\b|^master\s+status\b|^(?:show\s+)?agents\b",
          _fixed("/agents")),
    _rule(r"^agent\s+capacity\b|^how\s+much\s+agent\s+capacity\b", _fixed("/capacity")),
    _rule(r"^cancel\s+(?:all\s+)?agents\b", _fixed("/agentcancel")),
    _rule(r"^retry\s+(?:the\s+)?agent\b(?:\s+(?P<arg>.+))?",
          lambda m: ("/agentretry %s" % (m.group("arg") or "")).strip()),
    _rule(r"^(?:tool\s+)?activity\b|^recent\s+tools?\b|^what\s+tools\s+ran\b",
          _fixed("/activity")),
    _rule(r"^(?:orchestrate|master)\s+(?P<arg>.+)$", _with_arg("/master")),
    _rule(r"^autopilot\b(?:\s+(?P<arg>.+))?",
          lambda m: ("/autopilot %s" % (m.group("arg") or "")).strip()),

    # --- weather ---
    _rule(r"^(?:weather|forecast)\b(?:\s+(?:for|in)\s+(?P<arg>.+))?",
          lambda m: ("/weather %s" % (m.group("arg") or "")).strip()),

    # --- introspection ---
    _rule(r"^(?:chain\s+of\s+thought|your\s+thoughts|private\s+thoughts|"
          r"show\s+your\s+thoughts)\b", _fixed("/cot")),
    _rule(r"^(?:inspect\s+state|debug\s+state|debug\s+info|inspect\s+the\s+runtime)\b",
          _fixed("/debug")),
    _rule(r"^file\s+policy\b", _fixed("/filepolicy")),

    # --- file operations (require the word "file" or a path with an extension) ---
    _rule(r"^(?:find|list|search)\s+files?\b(?:\s+(?:matching|named|like)?\s*(?P<arg>.+))?",
          lambda m: ("/files %s" % (m.group("arg") or "")).strip()),
    _rule(r"^(?:read|open|show\s+me)\s+(?:the\s+)?file\s+(?P<arg>\S+)", _with_arg("/read")),
    _rule(r"^read\s+(?P<arg>\S+\.\w+)\s*$", _with_arg("/read")),
    _rule(r"^append\s+to\s+(?:file\s+)?(?P<arg>.+)$", _with_arg("/append")),
    _rule(r"^(?:write|save)\s+(?:to\s+)?file\s+(?P<arg>.+)$", _with_arg("/write")),
    _rule(r"^edit\s+file\s+(?P<arg>.+)$", _with_arg("/edit")),
    _rule(r"^delete\s+(?:the\s+)?file\s+(?P<arg>\S+)", _with_arg("/delete")),

    # --- todos ---
    _rule(r"^(?:show|list)\s+(?:my\s+)?(?:todos?|tasks)\b(?:\s+(?P<arg>.+))?",
          lambda m: ("/todo %s" % (m.group("arg") or "")).strip()),

    # --- run ---
    _rule(r"^run\s+in\s+(?:a\s+)?(?:new\s+)?(?:window|console)\b", _fixed("/runwindow")),
    _rule(r"^run\s+the\s+project\b", _fixed("/runproject")),

    # --- generation ---
    _rule(r"^(?:create|make|start|scaffold|generate)\s+(?:a\s+|an\s+)?(?:new\s+)?"
          r"(?P<kind>[\w+#.-]+)\s+(?:console\s+)?project"
          r"(?:\s+(?:named|called)\s+(?P<name>[\w.-]+))?\s*$",
          _scaffold_action),
    _rule(r"^(?:generate|make|create|build)\s+a\s+game\b(?:\s+(?P<arg>.+))?",
          lambda m: ("/game %s" % (m.group("arg") or "")).strip()),
    _rule(r"^(?:game\s+)?(?:forge|reference\s+suite|game\s+suite)\b(?:\s+(?P<arg>.+))?",
          lambda m: ("/forge %s" % (m.group("arg") or "")).strip()),
    _rule(r"^(?:generate|make|create)\s+(?:an?\s+)?(?:asset|artifact|image)\b"
          r"(?:\s+(?P<arg>.+))?", lambda m: ("/asset %s" % (m.group("arg") or "")).strip()),

    # --- model / persona ---
    _rule(r"^(?:switch\s+to|use|select|set)\s+(?:the\s+)?(?P<arg>(?!the\b|a\b)[\w.:-]+)\s+"
          r"(?:tier|model)\b", _with_arg("/model")),
    _rule(r"^set\s+(?:the\s+)?(?:persona|voice)\s+to\s+(?P<arg>.+)$", _with_arg("/persona")),
    _rule(r"^set\s+(?:the\s+)?model\s+to\s+(?P<arg>.+)$", _with_arg("/model")),

    # --- help ---
    _rule(r"^(?:help|what\s+can\s+you\s+do|show\s+help)\b", _fixed("/help")),
]


def resolve(text):
    """Return a synthesized slash-command line for `text`, or None.

    None means "not a command" -- the caller should fall through to its normal
    natural-language handling. A returned string is a slash line the caller can
    dispatch exactly as if the user had typed it.
    """
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if not value or value.startswith("/"):
        return None

    # The tier-aware trio owns its argument extraction; reuse it so there is one
    # source of truth for "second opinion on X" / "which model for Y" / "improve
    # fn in file". Its arg is already in the slash form's shape.
    tier = intents.classify_command(value)
    if tier and tier.get("command") in _TIER_COMMANDS:
        return ("/%s %s" % (tier["command"], tier["arg"])).strip()

    for pattern, action in _RULES:
        match = pattern.match(value)
        if match:
            result = action(match)
            if result:
                return result.strip()
    return None
