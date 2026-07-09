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
        "name": "/run",
        "category": "execution",
        "risk": "ask",
        "summary": "Run the previous fenced code block with a timeout.",
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
        "name": "/qualityfix",
        "category": "memory",
        "risk": "ask",
        "summary": "Dry-run or apply exact duplicate lesson cleanup.",
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
