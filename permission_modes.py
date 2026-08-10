"""Autonomy modes -- and the privilege axis they deliberately do not control.

Sonder already had per-tool ``allow``/``ask``/``deny`` rules in
``permission_rules``, but nothing ever called ``check()``: the policy was
displayed by ``/permissions`` and enforced nowhere. This module supplies the
missing decision point, and layers a mode on top of it so the same rule set can
mean "ask me about everything" while exploring and "just do it" while grinding.

Two axes, kept apart on purpose
-------------------------------
**Autonomy** (this module's MODES) is how often Sonder stops to ask.
**Privilege** (``elevated``) is what it is allowed to touch at all.

They are not the same question, and folding them into one dial is how you end
up with "auto" silently implying "administrator" -- the single worst
combination, because the setting people reach for when they want fewer prompts
is also the one that widens the blast radius. So no mode grants elevation; it
is always a separate, deliberate, session-scoped act.

Why four modes
--------------
Each is distinguished by a *different* boundary, so none is redundant:

    plan         nothing but reads. The mode for "tell me what you would do".
    manual       ask before anything that is not a read.          (default)
    acceptEdits  file changes flow; running host programs still asks.
    auto         host programs flow too.

The boundary between ``acceptEdits`` and ``auto`` is execution, not risk --
editing a file is recoverable from git, launching a process is not.

Why ``dangerous`` always asks
-----------------------------
Claude Code offers a full bypass. This deliberately does not: in every mode,
including ``auto``, the tools the catalog classes ``dangerous`` (``file_delete``,
``sqlite_mutate``, ``task_delete``, ``git_merge``, ``self_heal_repair``,
``admin_register``/``admin_set_account``, and the policy-changing
``runtime_policy_update``/``permission_rule_set``) still stop and ask.

Note the qualifier: *what the catalog classes dangerous*, which is not every
``admin_*`` tool. Read-only ones such as ``admin_whoami`` are ordinary ``ask``
tools and do flow in ``acceptEdits``/``auto``. Sonder's agent
loop can be driven by a 7B local model whose measured caller-judged accuracy is
around 53%; "never prompt me again" plus an unattended fleet plus irreversible
tools is not a combination worth shipping as a single keystroke. A standing
exemption for a specific tool is still available -- as an explicit, persistent,
auditable rule via ``permission_rule_set`` -- which is the right shape for that
decision because it is narrow and it is written down.

Enforcement scope
-----------------
``decide()`` is a pure function; call sites opt in. Interactive surfaces (the
REPL, the agent loop) can honour ``ask`` by actually asking. A direct MCP tool
call has no one to ask, so ``ask`` degrades to ``allow`` there unless the mode
is ``plan`` -- whose entire purpose is to hold still, and which therefore denies
everywhere. Preserving existing behaviour by default matters here: these rules
have been dormant since they were written, and switching them on globally in
one step would break flows that have always worked.

Stdlib only. ``command_catalog`` is imported lazily (it imports ``server``).
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass

# --- modes ----------------------------------------------------------------

PLAN = "plan"
MANUAL = "manual"
ACCEPT_EDITS = "acceptEdits"
AUTO = "auto"

# Cycle order for the keybinding: least autonomy -> most.
MODES = (PLAN, MANUAL, ACCEPT_EDITS, AUTO)
DEFAULT_MODE = MANUAL

ALLOW, ASK, DENY = "allow", "ask", "deny"

MODE_LABELS = {
    PLAN: "plan",
    MANUAL: "manual",
    ACCEPT_EDITS: "accept edits",
    AUTO: "auto",
}

MODE_BLURBS = {
    PLAN: "reads only - no writes, no commands",
    MANUAL: "ask before anything that is not a read",
    ACCEPT_EDITS: "file changes proceed; running programs still asks",
    AUTO: "file changes and programs proceed; destructive still asks",
}

# Colour hints for whatever renders the indicator (ANSI 256 palette).
MODE_COLOURS = {PLAN: 117, MANUAL: 250, ACCEPT_EDITS: 114, AUTO: 221}

# risk class -> action, per mode. "execution" is a synthetic class: tools that
# start a host process, split out of `ask`/`mutation` so acceptEdits and auto
# differ by something real.
_MATRIX = {
    PLAN: {"safe": ALLOW, "ask": DENY, "mutation": DENY,
           "execution": DENY, "dangerous": DENY},
    MANUAL: {"safe": ALLOW, "ask": ASK, "mutation": ASK,
             "execution": ASK, "dangerous": ASK},
    ACCEPT_EDITS: {"safe": ALLOW, "ask": ALLOW, "mutation": ALLOW,
                   "execution": ASK, "dangerous": ASK},
    AUTO: {"safe": ALLOW, "ask": ALLOW, "mutation": ALLOW,
           "execution": ALLOW, "dangerous": ASK},
}

# Tools that start a host process. Kept here rather than imported from server
# so this module stays importable on its own; a drift test cross-checks it
# against server's own execution sets.
EXECUTION_TOOLS = frozenset({
    "workspace_run", "script_run", "run_code", "run_project", "isolated_run",
    "build_run", "build_clean", "test_run", "lint_run", "format_code",
    "typecheck_run", "dependency_add", "dependency_remove", "dependency_update",
    "dependency_audit", "codegen_build_loop", "improve_function",
    "parallel_run_code", "parallel_generate_run", "parallel_generate_run_languages",
    "game_generate_and_test", "game_generation_campaign", "game_reference_suite",
    "campaign_generate_compile_execute_record", "campaign_repo_repair",
    "self_heal_repair", "scaffold_project",
})

# Tools that need OS administrator rights. Refused unless elevation is on.
PRIVILEGED_TOOLS = frozenset()

_LOCK = threading.RLock()
_STATE = {"mode": DEFAULT_MODE, "elevated": False, "elevation_reason": ""}
_LOADED = False


@dataclass(frozen=True)
class Decision:
    action: str          # allow | ask | deny
    mode: str
    risk: str
    reason: str
    tool: str = ""

    @property
    def allowed(self) -> bool:
        return self.action == ALLOW

    def to_dict(self) -> dict:
        return {"action": self.action, "mode": self.mode, "risk": self.risk,
                "reason": self.reason, "tool": self.tool}


# --- persistence ----------------------------------------------------------


def _state_path() -> str:
    try:
        import sonder_paths
        home = sonder_paths.default_home()
    except Exception:
        home = os.path.expanduser("~/.sonder")
    return os.path.join(home, "permission_mode.json")


def _load() -> None:
    global _LOADED
    with _LOCK:
        if _LOADED:
            return
        _LOADED = True
        try:
            with open(_state_path(), encoding="utf-8") as handle:
                saved = json.load(handle)
        except (OSError, ValueError):
            return
        mode = str(saved.get("mode", "")).strip()
        if mode in _MATRIX:
            _STATE["mode"] = mode
        # Elevation is deliberately NOT restored from disk. It is a decision
        # about this session made by whoever is at the keyboard now; silently
        # resuming it days later is exactly the surprise to avoid.


def _save() -> None:
    try:
        path = _state_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"mode": _STATE["mode"]}, handle)
    except OSError:
        pass


# --- mode state -----------------------------------------------------------


def current_mode() -> str:
    _load()
    with _LOCK:
        return _STATE["mode"]


def set_mode(name: str) -> str:
    """Set the mode by exact name or unambiguous prefix. Returns the new mode."""
    _load()
    wanted = str(name or "").strip().lower().replace(" ", "").replace("-", "")
    if not wanted:
        raise ValueError("mode name is required")
    match = None
    for mode in MODES:
        if mode.lower() == wanted:
            match = mode
            break
    if match is None:
        hits = [m for m in MODES if m.lower().startswith(wanted)]
        if len(hits) == 1:
            match = hits[0]
    if match is None:
        raise ValueError(
            "unknown mode '%s'. modes: %s" % (name, ", ".join(MODES))
        )
    with _LOCK:
        _STATE["mode"] = match
    _save()
    return match


def cycle_mode(step: int = 1) -> str:
    """Advance to the next mode. Backs the Shift+Tab keybinding."""
    _load()
    with _LOCK:
        index = MODES.index(_STATE["mode"])
        _STATE["mode"] = MODES[(index + step) % len(MODES)]
        new = _STATE["mode"]
    _save()
    return new


# --- privilege axis -------------------------------------------------------


def elevated() -> bool:
    with _LOCK:
        return bool(_STATE["elevated"])


def set_elevated(on: bool, reason: str = "") -> bool:
    """Turn the privilege axis on or off. Never called by a mode change."""
    with _LOCK:
        _STATE["elevated"] = bool(on)
        _STATE["elevation_reason"] = str(reason or "") if on else ""
        return _STATE["elevated"]


def elevation_reason() -> str:
    with _LOCK:
        return _STATE["elevation_reason"]


def host_is_elevated() -> bool:
    """Whether this process actually holds administrator rights."""
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        try:
            return os.geteuid() == 0  # type: ignore[attr-defined]
        except AttributeError:
            return False


# --- classification -------------------------------------------------------


def risk_of(tool_name: str) -> str:
    """Risk class for a tool, from the command catalog, with execution split out.

    ``dangerous`` is resolved FIRST and deliberately outranks ``execution``.
    Checking execution first let a tool that is both -- ``self_heal_repair`` is
    in ``EXECUTION_TOOLS`` and in the catalog's dangerous set -- report as
    merely ``execution``, which ``auto`` allows outright. That silently broke
    the one guarantee this module advertises, so the precedence is now
    dangerous > execution > whatever the catalog says.
    """
    name = str(tool_name or "").strip().lstrip("/")
    if not name:
        # Fail closed. An empty name dispatches nothing today, but returning
        # "safe" here put a fail-OPEN default directly above a comment
        # promising the opposite.
        return "ask"
    catalogued = ""
    try:
        import command_catalog
        command = command_catalog.by_name("/" + name)
        if command is not None:
            catalogued = command.risk
    except Exception:
        pass
    if catalogued == "dangerous":
        return "dangerous"
    if name in EXECUTION_TOOLS:
        return "execution"
    # Unknown tool: treat as needing a prompt, never as safe.
    return catalogued or "ask"


def decide(tool_name: str, *, interactive: bool = True,
           mode: str | None = None) -> Decision:
    """Whether ``tool_name`` may run right now.

    ``interactive=False`` means nobody is present to answer a prompt (a direct
    MCP call). ``ask`` then degrades to ``allow`` -- preserving how Sonder has
    always behaved -- except under ``plan``, which denies regardless because
    holding still is the entire point of that mode.
    """
    active = mode or current_mode()
    if active not in _MATRIX:
        # Report the mode actually applied. Echoing an unknown name back in the
        # Decision put a mode that was never in effect into the audit trail.
        active = DEFAULT_MODE
    risk = risk_of(tool_name)
    action = _MATRIX[active].get(risk, ASK)
    name = str(tool_name or "").lstrip("/")

    if name in PRIVILEGED_TOOLS and not elevated():
        return Decision(DENY, active, risk, "needs elevation, which is off", name)

    if action == ASK and not interactive:
        return Decision(
            ALLOW, active, risk,
            "no interactive prompt available; %s tools are not blocked outside "
            "the console" % risk,
            name,
        )
    reasons = {
        ALLOW: "%s allows %s tools" % (MODE_LABELS.get(active, active), risk),
        ASK: "%s asks before %s tools" % (MODE_LABELS.get(active, active), risk),
        DENY: "%s forbids %s tools" % (MODE_LABELS.get(active, active), risk),
    }
    return Decision(action, active, risk, reasons[action], name)


# --- presentation ---------------------------------------------------------


def status_line(colour: bool = False) -> str:
    """Compact indicator for a prompt or status bar."""
    mode = current_mode()
    label = MODE_LABELS.get(mode, mode)
    if elevated():
        label += " +admin"
    if not colour:
        return label
    return "\x1b[38;5;%dm%s\x1b[0m" % (MODE_COLOURS.get(mode, 250), label)


def describe(mode: str | None = None) -> str:
    """Full explanation of a mode and what it permits, for /mode and /help."""
    active = mode or current_mode()
    if active not in _MATRIX:
        raise ValueError("unknown mode '%s'" % active)
    lines = [
        "%s -- %s" % (MODE_LABELS.get(active, active), MODE_BLURBS.get(active, "")),
        "",
    ]
    order = ("safe", "ask", "mutation", "execution", "dangerous")
    labels = {
        "safe": "read-only tools",
        "ask": "tools that touch the workspace",
        "mutation": "tools that change files",
        "execution": "tools that run host programs",
        "dangerous": "destructive/administrative tools",
    }
    width = max(len(labels[k]) for k in order)
    for key in order:
        lines.append("  %-*s  %s" % (width, labels[key], _MATRIX[active][key]))
    lines += [
        "",
        "  privilege: %s" % (
            "elevated" + (" (%s)" % elevation_reason() if elevation_reason() else "")
            if elevated() else "normal - no mode grants this"
        ),
    ]
    return "\n".join(lines)


def overview() -> str:
    """All modes, marking the active one. Backs `/mode` with no argument."""
    active = current_mode()
    lines = ["sonder permission modes", ""]
    width = max(len(MODE_LABELS[m]) for m in MODES)
    for mode in MODES:
        marker = ">" if mode == active else " "
        lines.append("  %s %-*s  %s" % (
            marker, width, MODE_LABELS[mode], MODE_BLURBS[mode],
        ))
    lines += [
        "",
        "  shift+tab cycles   /mode <name> sets   /mode <name> --explain details",
        "  destructive tools ask in every mode, including auto.",
    ]
    if elevated():
        lines.append("  PRIVILEGE: elevated%s" % (
            " - " + elevation_reason() if elevation_reason() else ""))
    return "\n".join(lines)
