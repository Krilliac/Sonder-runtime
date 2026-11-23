"""Pure foreign-tool discovery, CLI help/version parser, and knowledge-card synthesizer.

Pure domain module: stdlib only, zero I/O, zero subprocesses, zero network.
Enables Sonder to systematically figure out external/unfamiliar tools (e.g.
Blender, Inkscape, FFmpeg, Krita, Godot, Pandoc, ImageMagick, or any unknown
CLI/library) from live `--version`/`--help` output, Python package metadata,
local docs, and optional sanitized web snippets—without hallucinating flags.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# Strict safe query validation: letters, digits, spaces, hyphen, underscore, dot, plus.
# Rejects path separators, shell metacharacters, leading dashes, and traversal.
_SAFE_TOOL_QUERY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _ .+-]{0,63}$")
_SAFE_SUBCOMMAND_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,47}$")

# Friendly display-name aliases -> candidate executable stems & common Windows folder hints.
# This is purely a discovery convenience; ANY unknown tool name works identically via normalization.
_TOOL_HINTS: dict[str, dict[str, tuple[str, ...]]] = {
    "blender": {
        "binaries": ("blender",),
        "folders": ("Blender Foundation", "Blender"),
        "python_pkgs": ("bpy",),
    },
    "inkscape": {
        "binaries": ("inkscape",),
        "folders": ("Inkscape",),
        "python_pkgs": (),
    },
    "krita": {
        "binaries": ("krita", "krita_cli"),
        "folders": ("Krita (x64)", "Krita"),
        "python_pkgs": (),
    },
    "audacity": {
        "binaries": ("audacity",),
        "folders": ("Audacity",),
        "python_pkgs": (),
    },
    "ffmpeg": {
        "binaries": ("ffmpeg", "ffprobe"),
        "folders": ("ffmpeg", "Gyan"),
        "python_pkgs": ("ffmpeg-python",),
    },
    "imagemagick": {
        "binaries": ("magick", "convert"),
        "folders": ("ImageMagick",),
        "python_pkgs": ("Wand",),
    },
    "magick": {
        "binaries": ("magick", "convert"),
        "folders": ("ImageMagick",),
        "python_pkgs": ("Wand",),
    },
    "godot": {
        "binaries": ("godot", "godot4", "Godot_v4"),
        "folders": ("Godot",),
        "python_pkgs": (),
    },
    "gimp": {
        "binaries": ("gimp-console-2.10", "gimp-2.10", "gimp-console", "gimp"),
        "folders": ("GIMP 2", "GIMP 3", "GIMP"),
        "python_pkgs": (),
    },
    "7zip": {
        "binaries": ("7z", "7za"),
        "folders": ("7-Zip",),
        "python_pkgs": ("py7zr",),
    },
    "7-zip": {
        "binaries": ("7z", "7za"),
        "folders": ("7-Zip",),
        "python_pkgs": ("py7zr",),
    },
    "pandoc": {
        "binaries": ("pandoc",),
        "folders": ("Pandoc",),
        "python_pkgs": ("pypandoc",),
    },
    "libreoffice": {
        "binaries": ("soffice", "libreoffice"),
        "folders": ("LibreOffice",),
        "python_pkgs": (),
    },
    "vscode": {
        "binaries": ("code",),
        "folders": ("Microsoft VS Code",),
        "python_pkgs": (),
    },
    "vs code": {
        "binaries": ("code",),
        "folders": ("Microsoft VS Code",),
        "python_pkgs": (),
    },
    "unreal": {
        "binaries": ("UnrealEditor-Cmd", "UnrealBuildTool", "RunUAT"),
        "folders": ("Epic Games",),
        "python_pkgs": (),
    },
    "unity": {
        "binaries": ("Unity",),
        "folders": ("Unity", "Unity Hub"),
        "python_pkgs": (),
    },
    "duckdb": {
        "binaries": ("duckdb",),
        "folders": ("DuckDB",),
        "python_pkgs": ("duckdb",),
    },
    "graphviz": {
        "binaries": ("dot", "neato"),
        "folders": ("Graphviz",),
        "python_pkgs": ("graphviz",),
    },
    "yt-dlp": {
        "binaries": ("yt-dlp",),
        "folders": ("yt-dlp",),
        "python_pkgs": ("yt-dlp",),
    },
}

_VERSION_LINE_RE = re.compile(
    r"(?i)\b(?:version\s+)?v?\d+\.\d+(?:\.\d+)?(?:[-._a-z0-9+]+)?"
)

# Matches CLI flags across GNU/argparse/clap/cobra/Blender/ffmpeg/Windows styles:
#   -b, --background               Run in background (often used for UI-less rendering)
#   -P, --python <filename>        Run the given Python script file.
#   --output=FILE                  Write output to FILE
#   -i <url>                       Input file
_FLAG_LINE_RE = re.compile(
    r"^\s{0,8}"
    r"(?P<spec>"
    r"(?:-[A-Za-z0-9?]|--[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"(?:\s*,\s*(?:-[A-Za-z0-9?]|--[A-Za-z0-9][A-Za-z0-9._-]*))?"
    r"(?:[=\s]+(?:<[^>]{1,40}>|\[[^\]]{1,40}\]|[A-Z][A-Z0-9_.-]{1,24}))?"
    r")"
    r"(?:\s{2,}|\t+|:?\s+)(?P<desc>.+?)\s*$"
)

# Matches subcommand lines under a "Commands:" / "Subcommands:" header or git-style command groups.
_SUBCMD_HEADER_RE = re.compile(
    r"^\s*(?:available\s+|common\s+|core\s+|main\s+|possible\s+)?(?:sub)?commands?\s*:?\s*$|"
    r"^(?:start\s+a\s+|work\s+on\s+|examine\s+|grow,\s+mark|collaborate|actions?|tasks?|operations?)\b[^:\r\n]{0,60}:?\s*$",
    re.IGNORECASE,
)
_SECTION_HEADER_RE = re.compile(
    r"^[A-Za-z][A-Za-z0-9 _/-]{1,40}:\s*$"
)
_SUBCMD_LINE_RE = re.compile(
    r"^\s{1,8}(?P<name>[a-zA-Z][a-zA-Z0-9_:-]{1,31})(?:\s{2,}|\t+)(?P<desc>[A-Za-z0-9].+?)\s*$"
)
_USAGE_LINE_RE = re.compile(r"(?i)^\s*(?:usage|synopsis)\s*:\s*(.+)$")

# Automation capability signal detectors (matched against discovered flags + descriptions)
_CAPABILITY_SIGNALS: dict[str, re.Pattern[str]] = {
    "headless_or_batch": re.compile(
        r"(?i)(?:--background|\b-b\b|--headless|--batch|--no-?gui|--console|--noninteractive|--quiet|--batch-?mode|--without-?gui)"
    ),
    "embedded_scripting": re.compile(
        r"(?i)(?:--python(?:-expr|-console)?|\b-P\b|--script|--eval|--exec|--lua|--run-?script|--command|-c\s)"
    ),
    "structured_output": re.compile(
        r"(?i)(?:--json|--format|--output|\b-o\b|--export|--render-output|--yaml|--csv|--xml)"
    ),
    "dry_run_or_check": re.compile(
        r"(?i)(?:--dry-?run|--check|--validate|--verify|--lint|--pretend|\b-n\b)"
    ),
    "stdin_or_pipe": re.compile(
        r"(?i)(?:--stdin|\bfrom\s+stdin\b|\bstandard\s+input\b)"
    ),
}

_STOP_WORDS = frozenset({
    "the", "and", "for", "with", "from", "into", "that", "this", "use", "using",
    "how", "can", "tool", "run", "file", "files", "help", "show", "make", "get",
    "set", "via", "about", "figure", "out", "learn", "work", "need", "want",
})


@dataclass(frozen=True)
class NormalizedToolQuery:
    raw: str
    canonical: str
    candidate_binaries: tuple[str, ...]
    folder_hints: tuple[str, ...]
    python_packages: tuple[str, ...]


def validate_tool_query(tool: str) -> tuple[NormalizedToolQuery | None, str]:
    """Validate and normalize a foreign tool name safely (fail-closed)."""
    raw = str(tool or "").strip()
    if not raw:
        return None, "tool name is required (e.g. 'blender', 'ffmpeg', 'inkscape', 'pandoc')"
    if ".." in raw or "/" in raw or "\\" in raw:
        return None, "tool name must be a plain tool/program name, not a filesystem path"
    if not _SAFE_TOOL_QUERY_RE.fullmatch(raw):
        return None, "tool name contains unsupported characters; use only letters, digits, spaces, '-', '_', '.', '+'"

    low = raw.lower()
    # Strip trailing .exe if user typed blender.exe
    canonical = low[:-4] if low.endswith(".exe") else low
    compact = canonical.replace(" ", "-")
    underscored = canonical.replace(" ", "_").replace("-", "_")

    hint = _TOOL_HINTS.get(canonical) or _TOOL_HINTS.get(compact)
    if hint:
        bins = hint["binaries"]
        folders = hint["folders"]
        py_pkgs = hint["python_pkgs"]
    else:
        cand_list: list[str] = []
        for c in (canonical, compact, canonical.replace(" ", "")):
            if c and c not in cand_list:
                cand_list.append(c)
        bins = tuple(cand_list)
        folders = (raw, canonical, compact)
        py_pkgs = tuple(dict.fromkeys([canonical, compact, underscored]))

    return (
        NormalizedToolQuery(
            raw=raw,
            canonical=canonical,
            candidate_binaries=bins,
            folder_hints=folders,
            python_packages=py_pkgs,
        ),
        "",
    )


def validate_subcommand(subcommand: str) -> tuple[str, str]:
    """Validate an optional subcommand name for `<binary> <subcommand> --help` probing."""
    clean = str(subcommand or "").strip()
    if not clean:
        return "", ""
    if clean.startswith("-") or not _SAFE_SUBCOMMAND_RE.fullmatch(clean):
        return "", "subcommand must be a plain command word (letters, digits, '-', '_', ':')"
    return clean, ""


def _tokenize_goal(goal: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9]{2,}", str(goal or "").lower())
    return [t for t in tokens if t not in _STOP_WORDS]


def parse_cli_help(
    help_text: str,
    *,
    version_text: str = "",
    goal: str = "",
    max_flags: int = 40,
    max_subcommands: int = 30,
) -> dict[str, Any]:
    """Parse raw CLI `--help` and `--version` text into structured flags, subcommands, and capabilities."""
    ver_clean = ""
    for line in str(version_text or "").splitlines():
        s = line.strip()
        if s:
            ver_clean = s[:180]
            if _VERSION_LINE_RE.search(s):
                break

    usage_lines: list[str] = []
    flags: list[dict[str, str]] = []
    seen_specs: set[str] = set()
    subcommands: list[dict[str, str]] = []
    seen_subcmds: set[str] = set()
    in_subcommands = False

    lines = str(help_text or "").splitlines()
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue

        um = _USAGE_LINE_RE.match(line)
        if um and len(usage_lines) < 6:
            usage_lines.append(um.group(1).strip()[:200])
            continue

        if _SUBCMD_HEADER_RE.match(line):
            in_subcommands = True
            continue
        if in_subcommands and _SECTION_HEADER_RE.match(line) and not _SUBCMD_HEADER_RE.match(line):
            in_subcommands = False

        fm = _FLAG_LINE_RE.match(line)
        if fm:
            spec = re.sub(r"\s+", " ", fm.group("spec").strip())
            desc = fm.group("desc").strip()
            # Pull one continuation line if indented deeply
            if idx + 1 < len(lines) and lines[idx + 1].startswith(" " * 12) and not _FLAG_LINE_RE.match(lines[idx + 1]):
                cont = lines[idx + 1].strip()
                if cont and not cont.startswith("-"):
                    desc = f"{desc} {cont}"
            if spec not in seen_specs and len(flags) < 250:
                seen_specs.add(spec)
                flags.append({"flag": spec[:80], "description": desc[:220]})
            continue

        if in_subcommands:
            sm = _SUBCMD_LINE_RE.match(line)
            if sm:
                name = sm.group("name").strip()
                desc = sm.group("desc").strip()
                if name.lower() not in seen_subcmds and not name.startswith("-") and len(subcommands) < 120:
                    seen_subcmds.add(name.lower())
                    subcommands.append({"name": name, "description": desc[:200]})

    # Detect automation capabilities from the discovered flags + raw help text
    combined_flag_text = "\n".join(f"{f['flag']} {f['description']}" for f in flags) or str(help_text or "")[:12000]
    capabilities: dict[str, list[str]] = {}
    for cap_name, pattern in _CAPABILITY_SIGNALS.items():
        matched_flags: list[str] = []
        for f in flags:
            if pattern.search(f"{f['flag']} {f['description']}"):
                matched_flags.append(f["flag"])
                if len(matched_flags) >= 5:
                    break
        if matched_flags:
            capabilities[cap_name] = matched_flags
        elif pattern.search(combined_flag_text):
            capabilities[cap_name] = ["detected_in_help_text"]

    # Rank flags & subcommands by relevance to `goal`
    goal_tokens = _tokenize_goal(goal)
    relevant_flags: list[dict[str, Any]] = []
    relevant_subcommands: list[dict[str, Any]] = []
    if goal_tokens:
        scored_flags: list[tuple[int, dict[str, str]]] = []
        for f in flags:
            hay = f"{f['flag']} {f['description']}".lower()
            score = sum(2 if tok in f["flag"].lower() else 1 for tok in goal_tokens if tok in hay)
            if score > 0:
                scored_flags.append((score, f))
        scored_flags.sort(key=lambda pair: (-pair[0], pair[1]["flag"]))
        relevant_flags = [item for _, item in scored_flags[:15]]

        scored_sub: list[tuple[int, dict[str, str]]] = []
        for sub in subcommands:
            hay = f"{sub['name']} {sub['description']}".lower()
            score = sum(2 if tok in sub["name"].lower() else 1 for tok in goal_tokens if tok in hay)
            if score > 0:
                scored_sub.append((score, sub))
        scored_sub.sort(key=lambda pair: (-pair[0], pair[1]["name"]))
        relevant_subcommands = [item for _, item in scored_sub[:10]]

    return {
        "version": ver_clean,
        "usage": usage_lines,
        "total_flags_discovered": len(flags),
        "total_subcommands_discovered": len(subcommands),
        "automation_capabilities": capabilities,
        "relevant_flags": relevant_flags,
        "relevant_subcommands": relevant_subcommands,
        "flags_sample": flags[:max_flags],
        "subcommands_sample": subcommands[:max_subcommands],
    }


def synthesize_tool_card(
    query: NormalizedToolQuery,
    *,
    goal: str = "",
    subcommand: str = "",
    discovered_binaries: list[dict[str, str]],
    probe_status: str,
    parsed_help: dict[str, Any],
    python_package: dict[str, Any] | None = None,
    local_docs: list[dict[str, str]] | None = None,
    memory_notes: list[str] | None = None,
    web_research: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Assemble a grounded, actionable Tool Knowledge Card from observed evidence only."""
    installed = bool(discovered_binaries)
    caps = parsed_help.get("automation_capabilities") or {}
    rel_flags = parsed_help.get("relevant_flags") or []
    rel_sub = parsed_help.get("relevant_subcommands") or []
    sub_sample = parsed_help.get("subcommands_sample") or []

    workflow: list[str] = []
    if installed:
        primary_bin = discovered_binaries[0]["name"]
        if caps.get("headless_or_batch") and caps.get("embedded_scripting"):
            workflow.append(
                f"Automate `{primary_bin}` non-interactively using discovered headless flag(s) "
                f"{caps['headless_or_batch'][:2]} with scripting flag(s) {caps['embedded_scripting'][:2]}."
            )
        elif caps.get("headless_or_batch"):
            workflow.append(
                f"Run `{primary_bin}` in non-interactive/batch mode using {caps['headless_or_batch'][:2]}."
            )
        elif caps.get("embedded_scripting"):
            workflow.append(
                f"Drive `{primary_bin}` via scripting flag(s) {caps['embedded_scripting'][:2]}."
            )
        if rel_sub:
            workflow.append(
                f"Inspect goal-relevant subcommand help next: call `learn_tool(tool='{query.canonical}', subcommand='{rel_sub[0]['name']}')`."
            )
        elif sub_sample and not subcommand:
            workflow.append(
                f"Tool has {len(sub_sample)}+ subcommands (e.g. {', '.join(s['name'] for s in sub_sample[:5])}); "
                f"probe any subcommand with `learn_tool(tool='{query.canonical}', subcommand='<name>')`."
            )
        if rel_flags:
            workflow.append(
                f"Goal-matched flags observed in live --help: {', '.join(f['flag'] for f in rel_flags[:5])}."
            )
        if caps.get("dry_run_or_check"):
            workflow.append(
                f"Validate command safely first with discovered dry-run/check flag {caps['dry_run_or_check'][0]}."
            )
        if not workflow:
            if probe_status == "timed_out_gui_or_non_cli":
                workflow.append(
                    f"`{primary_bin}` is installed but timed out on `--help` (likely a GUI-first app). "
                    "Re-run with `research_web=true` (when web access is enabled) or inspect its local docs/scripting API."
                )
            else:
                workflow.append(
                    f"`{primary_bin}` is installed; inspect the discovered flags/usage above before executing via `workspace_run` (subject to approval)."
                )
    else:
        if python_package and python_package.get("installed"):
            workflow.append(
                f"No standalone CLI binary found, but Python package `{python_package['name']}` "
                f"({python_package.get('version', 'installed')}) IS installed locally — inspect its API via `symbol_verify` or use it from Python."
            )
        else:
            workflow.append(
                f"`{query.raw}` was not found on PATH, Windows App Paths, or standard install folders."
            )
            if not web_research:
                workflow.append(
                    f"Call `learn_tool(tool='{query.canonical}', goal='{goal or 'CLI usage'}', research_web=true)` "
                    "(or `web_search`) to research official CLI docs and installation options, or call `plan_fallback` for built-in alternatives."
                )

    research_queries = [
        f"{query.raw} command line CLI flags documentation {goal}".strip(),
        f"{query.raw} headless batch scripting automation example {goal}".strip(),
    ]

    # Compact playbook summary suitable for memory persistence (no raw user paths)
    ver_str = parsed_help.get("version") or (python_package.get("version") if python_package else "") or "unknown"
    top_flag_str = ", ".join(f["flag"] for f in (rel_flags or parsed_help.get("flags_sample") or [])[:8])
    playbook_summary = (
        f"Tool `{query.canonical}` (installed={installed}, version={ver_str}): "
        f"capabilities={sorted(caps.keys())}; "
        f"key flags=[{top_flag_str or 'none parsed'}]."
    )

    return {
        "ok": True,
        "tool": query.raw,
        "canonical": query.canonical,
        "subcommand": subcommand,
        "goal": goal,
        "installed": installed,
        "probe_status": probe_status,
        "discovered_binaries": discovered_binaries,
        "version": parsed_help.get("version") or "",
        "usage": parsed_help.get("usage") or [],
        "automation_capabilities": caps,
        "total_flags_discovered": parsed_help.get("total_flags_discovered", 0),
        "total_subcommands_discovered": parsed_help.get("total_subcommands_discovered", 0),
        "relevant_flags": rel_flags,
        "relevant_subcommands": rel_sub,
        "flags_sample": parsed_help.get("flags_sample") or [],
        "subcommands_sample": sub_sample,
        "python_package": python_package,
        "local_docs": local_docs or [],
        "memory_notes": memory_notes or [],
        "web_research": web_research or [],
        "recommended_workflow": workflow,
        "research_queries": research_queries,
        "playbook_summary": playbook_summary,
    }
