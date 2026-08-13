"""Readable command/tool inventory for console, app, and agents."""

import re

COMMANDS = [
    {
        "name": "/help",
        "category": "basic",
        "risk": "safe",
        "summary": "Show console commands.",
    },
    {
        "name": "/stats",
        "category": "learning",
        "risk": "safe",
        "summary": "Show learning counts and recent lessons.",
    },
    {
        "name": "/context",
        "category": "context",
        "risk": "safe",
        "summary": "Show context, summary, memory, and session health.",
    },
    {
        "name": "/contextsize",
        "category": "context",
        "risk": "safe",
        "summary": "Select requested virtual context up to the configured max.",
    },
    {
        "name": "/compact",
        "category": "context",
        "risk": "safe",
        "summary": "Preview context compaction and rollover recommendations.",
    },
    {
        "name": "/commands",
        "category": "inspect",
        "risk": "safe",
        "summary": "List commands by category, name, or risk.",
    },
    {
        "name": "/dump",
        "category": "inspect",
        "risk": "safe",
        "summary": "Write the current chat and debug state to a text file.",
    },
    {
        "name": "/activity",
        "category": "inspect",
        "risk": "safe",
        "summary": "Show active/latest tool calls, file changes, and response activity.",
    },
    {
        "name": "/work",
        "category": "agents",
        "risk": "ask",
        "summary": "Execute a guarded tool-using task with a checklist, validation, and end report.",
    },
    {
        "name": "/autopilot",
        "category": "agents",
        "risk": "ask",
        "summary": "Run a persistent local goal with evidence-aware checkpoints, bounded replans, host gates, pause, resume, and cancel.",
    },
    {
        "name": "/ensemble",
        "category": "agents",
        "risk": "ask",
        "summary": "Ask several local models the same question, then compound their answers into one, naming any disagreement.",
    },
    {
        "name": "/runtime",
        "category": "system",
        "risk": "ask",
        "summary": "Inspect or guarded-edit shared local model mappings and execution-lane tiers; cloud remains separate.",
    },
    {
        "name": "/updatecheck",
        "category": "repo",
        "risk": "safe",
        "summary": "Fetch and report the installed Sonder Git commit, newest canonical commit, timestamps, and whether this source checkout is behind.",
    },
    {
        "name": "/update",
        # Fast-forwards the live runtime source checkout.  This is separate
        # from the read-only /updatecheck command so its dangerous risk does
        # not over-gate normal status inspection.
        "category": "repo",
        "risk": "dangerous",
        "summary": "Safely fast-forward a clean canonical Sonder source checkout; never merges, rebases, or overwrites local work.",
    },
    {
        "name": "/hardware",
        "category": "system",
        "risk": "safe",
        "summary": "Detect live system RAM, GPU runtime, VRAM, and offload support.",
    },
    {
        "name": "/location",
        # `/location on` assigns the REPL's `location_consent`
        # (sonder_repl.py:1083), which `main()` then passes to `server.sonder`
        # on every later turn (sonder_repl.py:1510) -- so it grants approximate
        # IP-geolocation consent that is OFF by default and stays granted for
        # the rest of the session. That is "changes what a later call may do",
        # the same reasoning `/mcp` and `/goal` carry, not the read its old
        # display-only entry claimed ("reads the location-consent env flag" --
        # true only of the bare `/location` form).
        #
        # Without this entry the command inherits `catalog()`'s default for a
        # console command fronting no tool, `safe`, which `plan` allows -- so a
        # mode advertising "reads only" would let a session acquire a new
        # capability. Graded by the worst it can do, like every other command
        # the gate reads a risk for. `ask` rather than `mutation` because it
        # writes no file; the two are the same row of the mode matrix.
        "category": "system",
        "risk": "ask",
        "summary": "Show, grant, or revoke approximate IP-location consent for this session.",
    },
    {
        "name": "/training",
        # The argument goes to adaptive_training.command_text, which runs
        # that CLI main(): start, deploy, rollback, adopt-legacy,
        # release-alias. Deploying an adapter changes which weights every
        # later call uses. No registered tool fronts it, so this entry is
        # what the permission gate grades the command by.
        "category": "learning",
        "risk": "dangerous",
        "summary": "Plan, explicitly start, inspect, deploy, or roll back adaptive weight training.",
    },
    {
        "name": "/goal",
        # adopt/complete/abandon/note write to goal_store; only the default
        # "show" reads. Graded by the worst it can do, like every other
        # command the gate reads a risk for.
        "category": "system",
        "risk": "mutation",
        "summary": "Set, inspect, note, or close the persistent goal; review and adopt self-proposed goals.",
    },
    {
        "name": "/selfmod",
        # `deploy` and `rollback` os.replace() files in Sonder's own source
        # tree, so this command's blast radius is the interpreter running it.
        # It is also the risk the permission gate grades the command by
        # (command_catalog._UNREGISTERED_BRANCH_WORK): the work is done by
        # module functions that front no registered tool, so nothing else in
        # the derivation can see it.
        "category": "system",
        "risk": "dangerous",
        "summary": "Inspect, isolate, test, approve, deploy, or roll back auditable self-improvements.",
    },
    {
        "name": "/mcp",
        # `refresh` republishes the live MCP source and tool registry, so it
        # alters what every later call is allowed to do -- the reasoning
        # command_catalog._DANGEROUS applies to runtime_policy_update and
        # permission_rule_set. No registered tool fronts it, so this entry is
        # what the permission gate grades the command by.
        "category": "system",
        "risk": "dangerous",
        "summary": "Audit or refresh the atomic live MCP source and tool registry.",
    },
    {
        "name": "/learning",
        "category": "memory",
        "risk": "safe",
        "summary": "Show grounded outcome coverage, lesson provenance, distillation yield, and memory hygiene.",
    },
    {
        "name": "/report",
        "category": "inspect",
        "risk": "safe",
        "summary": "Show the latest grounded end report and replayable action transcript.",
    },
    {
        "name": "/checklist",
        "category": "planning",
        "risk": "safe",
        "summary": "Show the current or selected persistent work checklist.",
    },
    {
        "name": "/inventory",
        "category": "filesystem",
        "risk": "ask",
        "summary": "Summarize a guarded workspace with bounded traversal, manifests, sizes, and exclusions.",
    },
    {
        "name": "/tree",
        "category": "filesystem",
        "risk": "ask",
        "summary": "List a bounded tree under a guarded folder.",
    },
    {
        "name": "/search",
        "category": "filesystem",
        "risk": "ask",
        "summary": "Search text across bounded files under a guarded root.",
    },
    {
        "name": "/programs",
        "category": "execution",
        "risk": "ask",
        "summary": "Find executable programs available to the workbench.",
    },
    {
        "name": "/scripts",
        "category": "execution",
        "risk": "ask",
        "summary": "Find runnable scripts under a guarded root.",
    },
    {
        "name": "/image",
        "category": "inspect",
        "risk": "ask",
        "summary": "Inspect a guarded image's format, dimensions, size, and digest.",
    },
    {
        "name": "/mkdir",
        "category": "filesystem",
        "risk": "ask",
        "summary": "Create a directory inside a guarded root.",
    },
    {
        "name": "/runprogram",
        "category": "execution",
        "risk": "ask",
        "summary": "Run an approved executable with argv JSON, timeout, cwd, and bounded output.",
    },
    {
        "name": "/runscript",
        "category": "execution",
        "risk": "ask",
        "summary": "Run a known script type without shell interpolation and with bounded output.",
    },
    {
        "name": "/todo",
        "category": "planning",
        "risk": "safe",
        "summary": "List, add, update, and inspect visible task state.",
    },
    {
        "name": "/master",
        "category": "agents",
        "risk": "ask",
        "summary": "Run inline or delegated master/subagent orchestration.",
    },
    {
        "name": "/agents",
        "category": "agents",
        "risk": "safe",
        "summary": "Inspect live and recent agent activity.",
    },
    {
        "name": "/capacity",
        "category": "agents",
        "risk": "safe",
        "summary": "Show queued fleet ceiling and current RAM/CPU-bounded worker slots.",
    },
    {
        "name": "/agentcancel",
        "category": "agents",
        "risk": "ask",
        "summary": "Cooperatively cancel an active agent/master prefix or all active agents.",
    },
    {
        "name": "/agentretry",
        "category": "agents",
        "risk": "ask",
        "summary": "Explicitly rerun an interrupted, failed, or cancelled persisted master task.",
    },
    {
        "name": "/weather",
        "category": "web",
        "risk": "ask",
        "summary": "Get sourced live conditions and a short forecast for a city or ZIP.",
    },
    {
        "name": "/asset",
        "category": "creative",
        "risk": "ask",
        "summary": "Generate icons, media, textured morphing multi-clip GLB models, scenes, and packs from a brief.",
    },
    {
        "name": "/artifactcheck",
        "category": "creative",
        "risk": "safe",
        "summary": "Ground an artifact path with inferred or explicit format-specific validation recipes.",
    },
    {
        "name": "/forge",
        "category": "creative",
        "risk": "ask",
        "summary": "Build and run the dependency-free cross-language reference game suite.",
    },
    {
        "name": "/game",
        "category": "creative",
        "risk": "ask",
        "summary": "Generate, execute, repair, and ground a persistent 2D/2.5D/3D game.",
    },
    {
        "name": "/gamefleet",
        "category": "creative",
        "risk": "ask",
        "summary": "Run a bounded parallel game campaign with optional language/dimension targets.",
    },
    {
        "name": "/run",
        "category": "execution",
        "risk": "ask",
        "summary": "Run the previous fenced code block with a timeout.",
    },
    {
        "name": "/runwindow",
        "category": "execution",
        "risk": "ask",
        "summary": "Launch the previous fenced code block in a separate Windows console.",
    },
    {
        "name": "/runproject",
        "category": "execution",
        "risk": "ask",
        "summary": "Run a generated multi-file project in a temp workspace.",
    },
    {
        "name": "/train",
        "category": "learning",
        "risk": "ask",
        "summary": "Run grounded practice tasks and record outcomes; no weights change.",
    },
    {
        "name": "/quality",
        "category": "memory",
        "risk": "safe",
        "summary": "Audit lesson quality and duplicate rows.",
    },
    {
        "name": "/privacy",
        "category": "learning",
        "risk": "safe",
        "summary": "Review redacted path and credential-like lesson findings.",
    },
    {
        "name": "/privacyfix",
        "category": "learning",
        "risk": "dangerous",
        "summary": "Dry-run or explicitly delete selected privacy-flagged lesson IDs.",
    },
    {
        "name": "/embeddings",
        "category": "learning",
        "risk": "ask",
        "summary": "Dry-run or locally refresh missing, legacy, or incompatible lesson embeddings.",
    },
    {
        "name": "/qualityfix",
        "category": "memory",
        "risk": "ask",
        "summary": "Dry-run or apply exact duplicate lesson cleanup.",
    },
    {
        "name": "/emotion",
        "category": "persona",
        "risk": "safe",
        "summary": "Show, set, reset, or live-tune emotion/tone vectors.",
    },
    {
        "name": "/prefer",
        "category": "persona",
        "risk": "safe",
        "summary": "Show, teach, or forget learned user preferences.",
    },
    {
        "name": "/files",
        "category": "filesystem",
        "risk": "ask",
        "summary": "Find files under guarded roots.",
    },
    {
        "name": "/read",
        "category": "filesystem",
        "risk": "ask",
        "summary": "Read a guarded file.",
    },
    {
        "name": "/write",
        "category": "filesystem",
        "risk": "ask",
        "summary": "Create a guarded text file.",
    },
    {
        "name": "/edit",
        "category": "filesystem",
        "risk": "ask",
        "summary": "Replace text in a guarded file.",
    },
    {
        "name": "/delete",
        "category": "filesystem",
        "risk": "dangerous",
        "summary": "Dry-run delete and show the required confirmation token.",
    },
    {
        "name": "/permissions",
        "category": "security",
        "risk": "safe",
        "summary": "Show the effective permission decision: rule, active mode, and which governs.",
    },
    {
        "name": "/debug",
        "category": "inspect",
        "risk": "safe",
        "summary": "Show safe debug state without private chain-of-thought.",
    },
]


def _catalog_rows():
    """Every command the runtime actually answers, via command_catalog.

    Imported lazily: command_catalog reads COMMANDS above for its curated
    summaries, so a module-level import here would be circular.
    """
    import command_catalog

    return [
        {
            "name": command.name,
            "category": command.category,
            "risk": command.risk,
            "summary": command.summary,
            "aliases": list(command.aliases),
            "usage": command.usage(),
        }
        for command in command_catalog.catalog()
    ]


_DISCOVERY_STOP_WORDS = frozenset({
    "a", "an", "and", "by", "for", "from", "in", "of", "on", "or", "the", "to", "with",
})
_DISCOVERY_EQUIVALENCE_GROUPS = (
    frozenset({"task", "todo", "checklist", "plan"}),
    frozenset({"status", "state", "show", "list", "inspect"}),
)
_DISCOVERY_EQUIVALENTS = {
    word: group
    for group in _DISCOVERY_EQUIVALENCE_GROUPS
    for word in group
}


def _discovery_match_score(command, filter_text):
    """Score a human capability query against one catalog row.

    Slash names are often terse while users ask for a capability using several
    related words (for example ``task checklist status``).  Exact phrase
    matching made those queries appear empty even though the catalog contained
    every relevant command.  Keep the old exact match, then require every
    distinct meaningful concept with a small, explicit task/status vocabulary.
    """
    haystack = " ".join(
        str(command.get(key, ""))
        for key in ("name", "category", "risk", "summary", "aliases", "usage")
    ).lower()
    query = str(filter_text or "").strip().lower()
    if not query:
        return 1
    if query in haystack:
        return len(query)
    tokens = [
        token for token in re.findall(r"[a-z0-9_]+", query)
        if token not in _DISCOVERY_STOP_WORDS
    ]
    if not tokens:
        return 0
    # Tool names use underscores (``model_fanout``), while people naturally
    # ask with spaces (``fanout model``).  Treat the separator as a word break
    # for discovery without changing the canonical command name we display.
    words = frozenset(re.findall(r"[a-z0-9_]+", haystack.replace("_", " ")))
    concepts = []
    seen = set()
    for token in tokens:
        variants = _DISCOVERY_EQUIVALENTS.get(token, frozenset({token}))
        identity = tuple(sorted(variants))
        if identity not in seen:
            concepts.append(variants)
            seen.add(identity)
    score = sum(bool(words & variants) for variants in concepts)
    return score if score == len(concepts) else 0


def list_commands(filter_text=""):
    """Filter the live command surface, falling back to the curated seed.

    COMMANDS below is a hand-written list that drifted badly -- it described 59
    commands while the runtime answered 265 -- so it is now only the seed for
    curated summaries and the offline fallback, never the answer.
    """
    try:
        source = _catalog_rows()
    except Exception:
        source = [dict(command) for command in COMMANDS]
    f = (filter_text or "").strip().lower()
    scored_rows = []
    for command in source:
        score = _discovery_match_score(command, f)
        if score:
            scored_rows.append((score, dict(command)))
    return [row for _score, row in sorted(
        scored_rows, key=lambda item: (-item[0], item[1]["name"]),
    )]


def format_commands(filter_text=""):
    rows = list_commands(filter_text)
    title = "sonder command registry"
    if filter_text:
        title += " (filter=%s)" % filter_text
    lines = [title]
    if not rows:
        lines.append("  (no matching commands)")
        return "\n".join(lines)
    width = max(len(row["name"]) for row in rows)
    for row in sorted(rows, key=lambda r: (r["category"], r["name"])):
        lines.append(
            "  %-*s  %-10s %-9s %s"
            % (width, row["name"], row["category"], row["risk"], row["summary"])
        )
    return "\n".join(lines)
