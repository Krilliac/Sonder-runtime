"""Readable command/tool inventory for console, app, and agents."""

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
        "name": "/runtime",
        "category": "system",
        "risk": "ask",
        "summary": "Inspect or guarded-edit shared local model mappings and execution-lane tiers; cloud remains separate.",
    },
    {
        "name": "/mcp",
        "category": "system",
        "risk": "safe",
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
        "summary": "Generate general-purpose icons, images, audio, models, scenes, and packs from a brief.",
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
        "summary": "Run grounded self-training tasks and record outcomes.",
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
        "summary": "Dry-run or locally backfill missing lesson embeddings in a bounded batch.",
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
        "summary": "Inspect local permission rules and matching behavior.",
    },
    {
        "name": "/debug",
        "category": "inspect",
        "risk": "safe",
        "summary": "Show safe debug state without private chain-of-thought.",
    },
]


def list_commands(filter_text=""):
    f = (filter_text or "").strip().lower()
    rows = []
    for command in COMMANDS:
        haystack = " ".join(
            str(command.get(k, "")) for k in ("name", "category", "risk", "summary")
        ).lower()
        if not f or f in haystack:
            rows.append(dict(command))
    return rows


def format_commands(filter_text=""):
    rows = list_commands(filter_text)
    title = "trilobite command registry"
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
