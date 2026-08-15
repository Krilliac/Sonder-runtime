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

After every hand-written rule misses, a generic ``command_catalog`` match runs
so the other ~225 commands are reachable in plain language too ("scan for
secrets" -> ``/secret_scan``). The hand-written rules keep first refusal
because they extract arguments the generic path cannot. The generic path is
deliberately narrow -- see ``_catalog_match`` -- because its failure mode is
hijacking a real coding question.

Stdlib only.
"""
import re

import command_catalog
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
          r"^what\s+commands(?:\s+are\s+there)?\b", _fixed("/commands")),
    _rule(r"^(?:show\s+)?permissions?\b|^permission\s+policy\b", _fixed("/permissions")),
    # Checking source freshness is deliberately separate from `/update`: the
    # former is read-only, while the latter changes the running checkout and
    # keeps its explicit confirmation/authority boundary.
    _rule(r"^(?:check(?:\s+for)?|show)\s+(?:runtime\s+|sonder\s+)?updates?\s*\??$|"
          r"^(?:is|am)\s+(?:sonder|the\s+runtime)\s+(?:up\s+to\s+date|current)\s*\??$",
          _fixed("/updatecheck")),
    _rule(r"^(?:dump|save)\s+(?:the\s+)?(?:chat|debug)(?:\s+log|\s+dump)?\b"
          r"(?:\s+(?P<arg>\S+))?", lambda m: ("/dump %s" % (m.group("arg") or "")).strip()),

    # Source-update discovery is read-only, but must still use the dedicated
    # refresh path rather than letting a chat model guess the checkout state.
    # These are whole-turn forms with no captured prose: an instruction copied
    # out of a page (or a request to do more after checking) cannot become an
    # update check by accident.
    _rule(r"^(?:can\s+you\s+|could\s+you\s+|please\s+)?"
          r"check\s+(?:whether\s+)?(?:sonder\s+)?(?:is\s+)?up\s+to\s+date\s*[?!.]*$",
          _fixed("/updatecheck")),
    _rule(r"^(?:can\s+you\s+|could\s+you\s+|please\s+)?"
          r"check\s+for\s+(?:a\s+)?(?:sonder\s+)?updates?\s*[?!.]*$",
          _fixed("/updatecheck")),

    # --- quality / privacy / emotion / prefs / improvement ---
    _rule(r"^(?:fix|repair)\s+(?:the\s+)?(?:memory\s+)?quality\b", _fixed("/qualityfix apply")),
    _rule(r"^(?:show\s+)?(?:memory\s+)?quality(?:\s+report)?\b", _fixed("/quality")),
    _rule(r"^(?:fix|repair)\s+privacy\b", _fixed("/privacyfix")),
    _rule(r"^(?:privacy\s+review|review\s+privacy)\b", _fixed("/privacyreview")),
    _rule(r"^(?:backfill|build)\s+(?:the\s+)?embeddings\b", _fixed("/embedfix")),
    _rule(r"^(?:show\s+)?(?:emotions?|mood|emotion\s+vectors)\b", _fixed("/emotion")),
    _rule(r"^(?:show\s+)?(?:my\s+)?preferences?\b(?:\s+(?P<arg>.+))?",
          lambda m: ("/prefer %s" % (m.group("arg") or "")).strip()),
    # Self-inspection must use the host's deterministic improvement report,
    # rather than asking the chat model to recall stale diagnostics or stored
    # facts.  These are deliberately complete, question-shaped turns only;
    # a request to inspect a particular file or quoted page text still falls
    # through to the normal conversation/work routes.
    _rule(r"^(?:(?:please\s+)?(?:can|could)\s+you\s+|please\s+)"
          r"(?:inspect|audit)\s+yourself\s+"
          r"(?:and\s+)?(?:tell|show)\s+me\s+(?:the\s+)?next\s+"
          r"(?:grounded\s+)?improvements?\s*[?!.]*$", _fixed("/improve")),
    _rule(r"^(?:(?:please\s+)?(?:can|could)\s+you\s+|please\s+)?"
          r"(?:give|show|tell)\s+me\s+(?:the\s+)?(?:next\s+)?"
          r"grounded\s+improvements?\s*[?!.]*$", _fixed("/improve")),
    _rule(r"^what\s+do\s+you\s+think\s+about\s+(?:the\s+)?system\s+"
          r"you(?:'re|\s+are)\s+(?:currently\s+)?in\s*[?!.]*$",
          _fixed("/improve")),
    _rule(r"^(?:can|could)\s+you\s+find\s+something\s+to\s+do\s*[?!.]*$",
          _fixed("/improve")),
    _rule(r"^(?:show\s+)?(?:system\s+)?improvements?(?:\s+report)?\b|"
          r"^what\s+should\s+(?:you|the\s+system)\s+improve\b", _fixed("/improve")),

    # --- agents / orchestration ---
    _rule(r"^(?:show\s+)?agents?\s+status\b|^master\s+status\b|^(?:show\s+)?agents\b",
          _fixed("/agents")),
    _rule(r"^agent\s+capacity\b|^how\s+much\s+agent\s+capacity\b", _fixed("/capacity")),
    _rule(r"^cancel\s+(?:all\s+)?agents\b", _fixed("/agentcancel")),
    _rule(r"^retry\s+(?:the\s+)?agent\b(?:\s+(?P<arg>.+))?",
          lambda m: ("/agentretry %s" % (m.group("arg") or "")).strip()),
    _rule(r"^(?:show|list|what(?:'s| is)|my)\s+active\s+fanouts?\b",
          _fixed("/fanouts active")),
    _rule(r"^(?:show|list|what(?:'s| is)|my)\s+(?:recent\s+)?fanouts?\b",
          _fixed("/fanouts")),
    _rule(r"^(?:tool\s+)?activity\b|^recent\s+tools?\b|^what\s+tools\s+ran\b",
          _fixed("/activity")),
    # These are deliberately exact whole-turn lifecycle requests.  Listing a
    # goal/workflow is read-only; starting a saved workflow remains an
    # explicitly named request and still goes through the REPL's ordinary
    # ``workflow_run`` permission gate before any saved action can execute.
    _rule(r"^(?:show\s+(?:me\s+)?(?:my\s+)?|what(?:'s|\s+is)\s+(?:my\s+)?)"
          r"(?:(?:current|active)\s+)?goals?\s*[?!.]*$", _fixed("/goal")),
    _rule(r"^(?:show|list)\s+(?:my\s+)?(?:saved\s+)?workflows?\s*[?!.]*$",
          _fixed("/workflow_list")),
    _rule(r"^(?:run|start)\s+(?:the\s+)?(?:saved\s+)?workflow\s+"
          r"(?P<arg>[A-Za-z][A-Za-z0-9_-]{1,63})\s*[?!.]*$",
          lambda m: "/workflow_run " + m.group("arg").lower()),
    _rule(r"^(?:orchestrate|master)\s+(?P<arg>.+)$", _with_arg("/master")),
    _rule(r"^autopilot\b(?:\s+(?P<arg>.+))?",
          lambda m: ("/autopilot %s" % (m.group("arg") or "")).strip()),

    # --- weather ---
    _rule(r"^(?:weather|forecast)\b(?:\s+(?:for|in)\s+(?P<arg>.+))?",
          lambda m: ("/weather %s" % (m.group("arg") or "")).strip()),

    # --- environment ---
    _rule(r"^(?:show\s+(?:the\s+)?|what\s+)?(?:host\s+)?environment\b(?:\s+are\s+you\s+(?:on|in))?\s*\??$",
          _fixed("/env")),
    _rule(r"^what\s+(?:os|platform|system)\s+(?:are\s+you|is\s+this)\s+(?:on|running(?:\s+on)?)\s*\??$",
          _fixed("/env")),
    _rule(r"^(?:which|what)\s+(?:tools?|toolchains?|shells?|compilers?)\s+"
          r"(?:are|do\s+you\s+have)\s+(?:installed|available)\s*\??$", _fixed("/env")),
    _rule(r"^(?:what\s+)?version\s+(?:is|of)\s+(?P<arg>[A-Za-z][A-Za-z0-9+._-]{0,63})\s*\??$",
          _with_arg("/toolstatus")),
    _rule(r"^(?:show|inspect|check)\s+(?:me\s+)?(?:my\s+|this\s+|the\s+)?"
          r"(?:(?:gpu|graphics(?:\s+card)?)\s+)?hardware\b\s*\??$", _fixed("/hardware")),
    _rule(r"^what(?:'s|\s+is)\s+(?:my\s+|this\s+|your\s+)"
          r"(?:gpu|graphics(?:\s+card)?|hardware|(?:gpu\s+)?compute\s+capability)\b\s*\??$",
          _fixed("/hardware")),
    _rule(r"^what\s+(?:gpu|graphics\s+card)\s+(?:do\s+i|does\s+this|do\s+you)\s+have\s*\??$",
          _fixed("/hardware")),

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

    # --- repository inspection (read-only) ---
    # These map git-familiar phrasing onto the read-only repo_* tools. The
    # mutating git commands (/git_commit, /git_checkout, ...) are deliberately
    # NOT given natural forms here: they stay reachable only by naming them,
    # under the catalog's adjacency rule for risky commands. Every pattern is
    # a complete turn (resolve() uses fullmatch), so "git status and then
    # push" or a quoted "git status" from a web page falls through to the
    # ordinary chat/work path instead of dispatching.
    _rule(r"^(?:show\s+(?:me\s+)?(?:the\s+)?|what(?:'s|\s+is)\s+(?:the\s+)?)?"
          r"(?:git|repo|repository)\s+status\s*[?!.]*$", _fixed("/repo_status")),
    _rule(r"^is\s+the\s+(?:working\s+)?tree\s+clean\s*[?!.]*$", _fixed("/repo_status")),
    _rule(r"^(?:git\s+log|(?:show|list)\s+(?:me\s+)?(?:the\s+)?"
          r"(?:recent|last|latest)\s+commits?|"
          r"what\s+are\s+the\s+(?:recent|latest)\s+commits?)\s*[?!.]*$",
          _fixed("/repo_log")),
    _rule(r"^(?:git\s+diff|show\s+(?:me\s+)?(?:the\s+)?diff|"
          r"show\s+(?:me\s+)?(?:the\s+)?(?:uncommitted|unstaged|pending)\s+"
          r"changes)\s*[?!.]*$", _fixed("/repo_diff")),

    # --- health / diagnostics (read-only) ---
    # "run a health check on the production database" carries a target and is
    # real work; only the bare, whole-turn self-check forms resolve.
    _rule(r"^(?:run\s+)?(?:a\s+)?(?:health[\s-]?check|self[\s-]?check|"
          r"diagnostics)\s*[?!.]*$", _fixed("/diagnostics")),
    _rule(r"^are\s+you\s+healthy\s*[?!.]*$", _fixed("/diagnostics")),

    # --- test discovery (read-only; running tests stays /test_run) ---
    _rule(r"^(?:list|show|find|discover)\s+(?:me\s+)?(?:the\s+)?"
          r"(?:available\s+)?tests\s*[?!.]*$", _fixed("/test_discover")),
    _rule(r"^what\s+tests\s+(?:are\s+there|exist|do\s+we\s+have)\s*[?!.]*$",
          _fixed("/test_discover")),

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


# --- generic catalog fallback ---------------------------------------------
#
# The hand-written rules above cover ~40 commands; the catalog exposes 265.
# Rather than hand-write 225 more patterns (which would rot the moment a tool
# is added), the rest are matched generically against their own names, aliases
# and summaries. Everything below exists to keep that from firing on prose.
#
# A turn has to clear four independent gates to resolve generically:
#
# 1. OPENING. It must start with an imperative/request verb ("scan for
#    secrets") or name a multi-word command outright ("what's my task
#    progress" contains "task progress"). A question or a statement that does
#    neither is left alone, which is what keeps "how do I cache a parse
#    result" and "the build is broken, any idea why" out.
# 2. NAME. Every token of the command's name (or of one alias) must be
#    present, so a single word shared with a summary can never elect a
#    command. Summary words only break ties and, in the narrow second stage,
#    stand in for a partly-named command ("scan for leaked api keys").
# 3. COVERAGE. Every remaining content word in the turn must be accounted for
#    by that command's name or summary. This is what distinguishes "read the
#    file notes.txt" (a command) from "read the file notes.txt and summarize
#    it" (work): the leftover words prove the turn is asking for more than the
#    command does. One trailing leftover is allowed as an argument, and only
#    for a multi-word name that was matched in full.
# 4. UNIQUENESS. If two commands score identically the turn is ambiguous and
#    resolves to nothing, rather than to whichever happened to sort first.
#
# Mutation and dangerous commands additionally have to be named -- their name
# tokens must appear adjacent in the turn -- and can never be reached from the
# summary-only stage. "delete task abc" names /task_delete; "clean up" names
# nothing.

_WORD = re.compile(r"[a-z0-9]+")

# The three vocabularies are written as plain prose here and normalized into
# _STOPWORDS / _OPENERS / _POLITE below, once _norm_token exists: they have to
# live in the same token space as the turn or "status" never finds itself.

_RAW_STOPWORDS = """
a an the this that these those my your our their his her
is are was were be been being am do does did done doing has have had
to for of in on at by with from about into onto over under out up off
and or but not no nor so then than if when while as at
please just let lets us me i you we they it he she
all any some every each other another there here now again really very
what whats which who whom whose how why where whether
s t m d re ve ll
"""

# An explicit imperative/request opening. Deliberately excludes the verbs that
# introduce work rather than a command -- fix, write, implement, refactor,
# explain, reset -- because those open real requests ("fix the failing API
# tests", "reset the session token when it expires") far more often than they
# open a command.
_RAW_OPENERS = """
run rerun execute launch start stop restart pause resume retry cancel kill
show display print tell list ls dump export
check inspect audit review verify validate diagnose
scan search find locate grep look
get fetch pull read open view load
create make generate build compile scaffold add
delete remove drop clear
format lint typecheck test benchmark profile
commit diff compare status count measure
update set switch enable disable
"""

# Dropped from the front before looking for an opener, so "can you list the
# workflows" is still an imperative opening.
_RAW_POLITE = """
please can could would will you i we want need to like d lets let us
hey ok okay kindly pls go ahead and now just
"""

_MAX_POLITE_PREFIX = 4

# A summary word is "distinctive" only if few other commands use it, which is
# what stops "file", "run" or "code" from electing a command on their own.
_MAX_DISTINCTIVE_DF = 3

_RISKY = frozenset({"mutation", "dangerous"})

# Verbs that ask to be shown something. Naming a command is normally enough to
# reach it even when it mutates, but a read verb contradicts that: "list the
# git branches" names /git_branch, which CREATES one. When the opening verb
# asks to look and the command would change something, the turn is asking for
# a different thing than it named, so route nothing.
_READ_VERBS = frozenset("""
show display print tell list ls view read open
inspect review audit check count measure status
compare diff describe
""".split())

_INDEX_CACHE = {"key": None, "entries": ()}


_ES_STEMS = ("s", "x", "z", "ch", "sh")


def _norm_token(token):
    """Fold a trivial plural so "secrets" matches ``secret_scan``.

    The ``-es`` case matters more than it looks: without it "list processes"
    never reaches ``/process_list``.
    """
    if len(token) > 4 and token.endswith("es"):
        stem = token[:-2]
        if stem.endswith(_ES_STEMS):
            return stem
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _tokenize(text):
    return [_norm_token(t) for t in _WORD.findall(str(text or "").lower())]


def _word_set(raw):
    """The vocabularies live in the same normalized space as the turn.

    Without this a bare "status" tokenizes to "statu" and then fails to find
    itself in the opener list.
    """
    return frozenset(_norm_token(word) for word in raw.split())


_STOPWORDS = _word_set(_RAW_STOPWORDS)
_OPENERS = _word_set(_RAW_OPENERS)
_POLITE = _word_set(_RAW_POLITE)


def _index():
    """Per-command token sets, rebuilt when the catalog itself changes.

    ``command_catalog.catalog()`` is memoised and returns a new tuple after a
    live reload adds tools, so identity on that tuple is the cache key.
    """
    try:
        commands = command_catalog.catalog()
    except Exception:  # a half-initialised server must not break plain chat
        return ()
    if _INDEX_CACHE["key"] is commands:
        return _INDEX_CACHE["entries"]

    rows = []
    frequency = {}
    for command in commands:
        variants = []
        for name in command.all_names:
            tokens = tuple(_tokenize(name))
            if tokens and tokens not in variants:
                variants.append(tokens)
        summary = frozenset(
            t for t in _tokenize(command.summary) if t not in _STOPWORDS
        )
        for token in summary:
            frequency[token] = frequency.get(token, 0) + 1
        rows.append({"command": command, "variants": tuple(variants),
                     "summary": summary})
    for row in rows:
        row["distinctive"] = frozenset(
            t for t in row["summary"]
            if len(t) >= 3 and frequency.get(t, 0) <= _MAX_DISTINCTIVE_DF
        )
    entries = tuple(rows)
    _INDEX_CACHE["key"] = commands
    _INDEX_CACHE["entries"] = entries
    return entries


def _opening(tokens):
    """(imperative the turn opens with, tokens from that verb on).

    The politeness a request is wrapped in is dropped rather than ignored:
    "can you list the workflows" has to reduce to "list the workflows" or the
    coverage check counts "can" as an unexplained word and refuses the turn.
    """
    for index, token in enumerate(tokens[:_MAX_POLITE_PREFIX + 1]):
        if token in _OPENERS:
            return token, tokens[index:]
        if token not in _POLITE:
            return None, tokens
    return None, tokens


def _names_a_command(tokens, entries):
    """True when a multi-word command name appears verbatim in the turn.

    Only multi-word names count. A one-word name ("/build", "/status",
    "/loop") is an ordinary English word, and letting one open the gate would
    hand every sentence mentioning a build to the router.
    """
    for entry in entries:
        for variant in entry["variants"]:
            width = len(variant)
            if width < 2:
                continue
            for start in range(len(tokens) - width + 1):
                if tuple(tokens[start:start + width]) == variant:
                    return True
    return False


def _adjacent(tokens, variant):
    """The name's tokens sit together, allowing one filler word inside."""
    wanted = set(variant)
    for width in (len(variant), len(variant) + 1):
        for start in range(max(0, len(tokens) - width + 1)):
            if wanted <= set(tokens[start:start + width]):
                return True
    return False


def _score(entry, present, content_set):
    """(stage, strength) for one command, or None when it is not a candidate.

    Stage 2 is a full name match -- every word of the command's name (or an
    alias) is in the turn. Stage 1 is the narrow summary fallback: part of the
    name plus two or more distinctive summary words, which is what lets "scan
    for leaked api keys" find ``/secret_scan``.
    """
    full = 0
    partial = 0
    matched = ()
    for variant in entry["variants"]:
        hits = sum(1 for token in variant if token in present)
        partial = max(partial, hits)
        if hits == len(variant) and len(variant) > full:
            full, matched = len(variant), variant
    summary_hits = len(content_set & entry["summary"])
    if full:
        return 2, (full, summary_hits), matched
    if partial and entry["command"].risk not in _RISKY:
        distinctive = len(content_set & entry["distinctive"])
        if distinctive >= 2:
            return 1, (distinctive, summary_hits), ()
    return None


def _catalog_match(value):
    """Resolve `value` against the whole command catalog, or return None.

    Conservative by construction: see the gate list at the top of this
    section. Returning None is always the safe answer -- the caller falls
    through to normal chat, which can still run the command itself.
    """
    entries = _index()
    if not entries:
        return None
    tokens = _tokenize(value)
    if not tokens:
        return None
    lead, body = _opening(tokens)
    if lead is None and not _names_a_command(tokens, entries):
        return None

    content = [t for t in body if t not in _STOPWORDS]
    if not content:
        return None
    present = set(body)
    content_set = set(content)

    ranked = []
    for entry in entries:
        scored = _score(entry, present, content_set)
        if scored:
            ranked.append((scored[0], scored[1], entry, scored[2]))
    if not ranked:
        return None

    top = max(rank[:2] for rank in ranked)
    finalists = [rank for rank in ranked if rank[:2] == top]
    if len(finalists) != 1:
        # A tie means the turn does not pick a command out; guessing here is
        # exactly the failure this fallback must not have.
        return None
    stage, _strength, entry, matched = finalists[0]
    command = entry["command"]

    if command.risk in _RISKY and (stage != 2 or not _adjacent(tokens, matched)):
        # Destructive and file-changing commands are never inferred from a
        # loose match; the turn has to name them.
        return None
    if command.risk in _RISKY and lead in _READ_VERBS:
        # Asked to be shown something, but the named command would change it.
        return None

    explained = set(entry["summary"])
    for variant in entry["variants"]:
        explained.update(variant)
    if lead and len(matched) >= 2:
        # The request verb frames the ask rather than adding content -- but
        # only once the turn has named a multi-word command. A one-word name
        # is weak evidence and has to account for the whole turn, verb
        # included, or "create a file" would resolve to /files.
        explained.add(lead)
    leftover = [t for t in content if t not in explained]
    if not leftover:
        return command.name
    required = any(p.required for p in command.params)
    if (len(leftover) == 1 and len(matched) >= 2 and required
            and leftover[0] == content[-1]):
        # One trailing word after a fully named multi-word command that wants
        # an argument is that argument ("delete task abc"). Anything more, a
        # bare one-word name, or a command that takes nothing, means the turn
        # is asking for more than the command does.
        words = re.findall(r"[A-Za-z0-9][A-Za-z0-9_.\-]*", str(value))
        if words and _norm_token(words[-1].lower()) == leftover[0]:
            return "%s %s" % (command.name, words[-1])
    return None


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
        # A natural-language command must consume the whole turn. Prefix
        # matches turned prose such as "reset the session token when it
        # expires" into destructive lifecycle commands and silently discarded
        # everything after a file path.
        match = pattern.fullmatch(value)
        if match:
            result = action(match)
            if result:
                return result.strip()

    # Nothing hand-written claimed the turn: fall back to the catalog so the
    # commands nobody wrote a pattern for are still reachable in plain
    # language. This runs last so the rules above keep their argument
    # extraction and their precedence.
    generic = _catalog_match(value)
    return generic.strip() if generic else None
