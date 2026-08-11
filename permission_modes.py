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

Read that as the promise it is, which is narrower than "cannot happen":
``ask`` is a promise about a *prompt*, and a prompt needs somebody to answer
it. Three of the five call sites below pass ``interactive=False`` -- the agent
path, the loop path and the MCP entry point -- and there ``ask`` degrades to
``allow`` (see "Enforcement scope"), so ``auto`` plus no operator runs a
``dangerous`` tool without stopping. ``plan`` is the exception that holds
everywhere. That degrade is deliberate and is what "preserve current
behaviour" requires; what is not acceptable is stating the guarantee without
it, so ``ASK_CAVEAT`` carries the condition onto every surface that repeats
the claim.

Note the other qualifier: *what the catalog classes dangerous*, which is not every
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
``decide()`` is a pure function; call sites opt in. Every place a tool is
chosen -- by a model, by a person, or by a protocol client -- does:

    server._agent_dispatch          the agent/workbench/autopilot tool path,
                                    via ``_agent_permission_gate_error``.
                                    ``interactive=False``.
    server._loop_dispatch           the ``loop``/``workflow_run`` actions a
                                    model authors, via
                                    ``_loop_permission_refusal``.
                                    ``interactive=False``.
    reloadable_mcp.call_tool        the protocol entry point an MCP client
                                    arrives at, via ``_refuse_if_gated``.
                                    ``interactive=False``.
    sonder_repl.main                the ~50 hand-written ``/write``-style
                                    branches and the ~25 forwarded to
                                    ``server.control_command``, at one choke
                                    point via ``_named_command_gate``.
                                    ``interactive`` iff stdin is a terminal.
    sonder_repl._run_catalogued     ``/<tool_name>`` typed at the console --
                                    the fallback for everything without a
                                    hand-written branch, via
                                    ``_permission_gate``. Same, via the same
                                    ``_gate_tools`` seam.

The console's two entries are disjoint by construction: a typed command is
served by a named branch or by the catalogued fallback, never both, so nothing
is prompted for twice.

``interactive`` means "somebody is present to answer", which is why the console
sites compute it (``sonder_repl._console_has_operator``) rather than assert it.
`sonder < script.txt` is a console session with nobody at the keyboard, and
there ``input()`` does not ask anyone anything -- it consumes the next line of
the script. A piped console therefore degrades exactly like a protocol caller.

Interactive surfaces honour ``ask`` by actually asking (the console prompts
``y/N``, defaulting to no). A caller with no one to ask degrades ``ask`` to
``allow`` unless the mode is ``plan`` -- whose entire purpose is to hold still,
and which therefore denies everywhere. Preserving existing behaviour by default
matters here: these rules have been dormant since they were written, and
switching them on globally in one step would break flows that have always
worked. That is what ``interactive=False`` buys: under the default ``manual``
mode such a caller refuses nothing the mode itself refused before, and only
``plan`` or an explicit per-tool ``deny`` rule can stop a call.

The gate sits at those entry points and *not* inside the tool functions
themselves. An internal Python call to ``server.file_write`` is therefore
ungated -- deliberately, because the surfaces above each call the function
directly with their own ``interactive`` value, and gating the bodies as well
would prompt twice for one console command and attribute the agent path's
refusal to the wrong layer.

One deliberate exemption, at the two surfaces a *person* drives Sonder
through: ``permission_mode`` itself is never gated at the console
(``sonder_repl.GATE_EXEMPT_TOOLS``) or at the MCP protocol entry point
(``reloadable_mcp._refuse_if_gated``, which consults the same
``GATE_CONTROL_TOOLS``). It is risk ``ask``, which ``plan`` denies, and the
chosen mode persists to disk -- so gating it would leave whoever selected
``plan`` unable to select anything else, across restarts, with no remedy but
hand-editing ``permission_mode.json``.

Be exact about what that costs, because the exemption is about who can defeat
the gate. Sonder's *own* agent and loop paths get no exemption -- a model
Sonder is running must not be able to lift its own restraint, and
``_agent_dispatch`` cannot reach the tool at all. But an external model
driving Sonder over MCP reaches ``reloadable_mcp`` and therefore *can* lift
``plan``. That is the accepted price of not trapping an operator whose only
client is an MCP one; it is not an accident, and it is not "console-only".

Rules and modes compose; they do not race
-------------------------------------------
``permission_rules`` shipped its own per-tool ``allow``/``ask``/``deny`` rules
long before this module existed, but nothing ever called ``check()`` -- the
policy was displayed by ``/permissions`` and enforced nowhere. ``decide()``
now looks one up on every call, through a small swappable hook
(``_rule_lookup``, module-level, defaulting to ``_default_rule_lookup``, which
reads the real on-disk policy via ``permission_rules`` +
``sonder_paths.default_home()``). A caller may also pass ``rule_lookup=``
directly, which takes precedence over the module hook for that call; this is
what lets ``decide()`` stay testable without ever touching a real, machine-
specific ``permissions.json``.

The combination follows one explicit precedence, in this order:

    1. an explicit rule DENY   always wins -- over every mode, including auto.
    2. privilege                (``PRIVILEGED_TOOLS`` or a per-call
                                 ``requires_elevation=True`` + ``elevated()``)
                                 is checked next; a rule cannot grant
                                 elevation, the same way no mode can.
    3. an explicit rule ALLOW  satisfies the mode's ASK -- it loosens an ask
                                 into an allow, but never touches a mode that
                                 already allows or (under ``plan``) denies.
    4. plan's denials          are never overridden by a rule -- holding still
                                 is that mode's entire purpose, so an allow
                                 rule is inert there.
    5. anything else           (no rule matched, or a matched rule says
                                 ``ask``, or is otherwise unrecognised) is
                                 inert: the mode alone decides, byte-for-byte
                                 what it decided before rules were wired in.

Why deny outranks every mode, including auto: a rule is a narrower, written
decision about ONE tool (``permission_rule_set file_delete deny``); a mode is
a broad dial covering five risk classes at once for everything Sonder can do.
The narrower, audited, on-the-record decision should outrank the broad one --
which is also why an allow rule only ever *loosens* an ask and never a deny:
letting it loosen a deny would mean a five-risk-class dial (or ``plan``'s
"hold still") could be defeated by a rule that was written down for some
unrelated tool's convenience. ``Decision.reason`` always says which of these
layers actually decided, so an operator can tell a mode refusal from a rule
refusal from an elevation refusal, and ``Decision.source`` says the same thing
in one word (``rule``/``mode``/``privilege``/``non-interactive``) for anything
that needs to branch on it rather than print it -- notably ``/permissions``,
which must show *which* of the rule and the mode governs a tool. That answer is
produced here, once, where the precedence above is implemented; re-deriving it
at a display surface would be a second copy of this table, free to drift from
the one that enforces.

Why ``PRIVILEGED_TOOLS`` is empty
----------------------------------
``workspace_run`` running ``dism`` genuinely failed with Windows error 740
("elevated permissions are required"). That is evidence a *command* can need
administrator rights -- it is not evidence that ``workspace_run``, or any
other Sonder tool, is inherently privileged. The same ``workspace_run`` also
runs ``dir``, ``git status``, a linter; none of those need anything, and that
is true of every tool in ``EXECUTION_TOOLS``. Privilege here is a property of
what the caller asks a general tool to do, not a fixed property of the tool,
so no tool is named in ``PRIVILEGED_TOOLS``. Marking ``workspace_run`` (or
any execution tool) privileged outright would deny the overwhelming majority
of its ordinary, unprivileged calls -- training operators to route around
the gate rather than trust it, which is the same "refusal nobody can act on"
failure the ``dangerous`` class above exists to avoid. See
``requires_elevation`` on ``decide()`` for how a specific invocation --
rather than a tool name -- gates on privilege instead, and ``/elevate`` (the
``elevate`` tool in ``server.py``) for how a person actually turns the axis
on. ``PRIVILEGED_TOOLS`` is kept live and exercised by
``test_privileged_tools_are_denied_without_elevation`` precisely so that the
day a tool genuinely does require administrator rights just to be invoked,
naming it here is a one-line change into an already-tested path.

Stdlib only. ``command_catalog`` is imported lazily (it imports ``server``);
``permission_rules`` and ``sonder_paths`` are imported lazily for the same
reason -- so this module keeps importing on its own with no cycle.
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

# The one sentence that keeps every "ask" claim on this module's surfaces
# honest, defined once so the copies cannot drift. `ask` is a promise about a
# prompt; a caller with nobody to prompt gets `allow` instead, except under
# `plan`. `server._permission_mode_context` prints this on every
# `/permissions` render and the presentation functions below repeat it, so a
# reader of any of them can get from a row to their own answer.
ASK_CAVEAT = (
    "'ask' means a prompt at the console; a caller with nobody to ask "
    "proceeds instead, except under plan."
)

MODE_BLURBS = {
    PLAN: "reads only - no writes, no commands",
    MANUAL: "ask before anything that is not a read",
    ACCEPT_EDITS: "file changes proceed; running programs still asks",
    # "destructive still asks" full stop was false for the three
    # `interactive=False` call sites, and this string has the widest reach of
    # any of them: `server.permission_mode_data()` ships it to the Flutter
    # client, which renders it on the mode chip and in the mode picker. Name
    # where the prompt happens, because it does not happen anywhere else.
    AUTO: "file changes and programs proceed; destructive still asks at the console",
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

# Tools that need OS administrator rights UNCONDITIONALLY -- just being
# invoked, regardless of what they are asked to do. Refused unless elevation
# is on. Deliberately empty; see "Why PRIVILEGED_TOOLS is empty" above before
# adding to this. A per-call need for elevation belongs in
# ``requires_elevation=True`` at the ``decide()`` call site instead.
PRIVILEGED_TOOLS = frozenset()

# The gate's own control, exempt at every surface a *person* reaches the gate
# through: the console and the MCP protocol entry point. ``permission_mode`` is
# risk ``ask``, which ``plan`` denies, and the chosen mode persists to disk --
# so gating it would leave whoever selected ``plan`` unable to select anything
# else, across restarts, with no remedy but hand-editing
# ``permission_mode.json``. A refusal nobody can act on is the failure this
# module's ``dangerous``-always-asks note already argues against, and it is
# worse here because the refusal message names the very tool it is refusing.
#
# ``server._agent_dispatch`` and ``server._loop_dispatch`` deliberately do NOT
# consult this: a model must not be able to lift its own restraint. The
# distinction is who is choosing -- a person driving Sonder, or Sonder driving
# itself -- not how hard the tool is to reach.
GATE_CONTROL_TOOLS = frozenset({"permission_mode"})

_LOCK = threading.RLock()
_STATE = {"mode": DEFAULT_MODE, "elevated": False, "elevation_reason": ""}
_LOADED = False


@dataclass(frozen=True)
class Decision:
    """One gate decision, including *which layer* actually made it.

    ``reason`` is prose for a person; ``source`` is the same fact in a form
    something else can branch on. It exists because ``/permissions`` has to
    tell an operator whether a rule or the mode governs a tool, and the only
    honest place to answer that is here, where the precedence is implemented.
    A renderer that re-derived it from ``(rule, mode)`` would be a second copy
    of the precedence table, free to drift from the one that enforces.
    """

    action: str          # allow | ask | deny
    mode: str
    risk: str
    reason: str
    tool: str = ""
    # rule | mode | privilege | non-interactive -- the layer that decided.
    # Defaulted to the commonest case so the field cannot be forgotten into a
    # crash, but every return site in decide() sets it explicitly.
    source: str = "mode"

    @property
    def allowed(self) -> bool:
        return self.action == ALLOW

    def to_dict(self) -> dict:
        return {"action": self.action, "mode": self.mode, "risk": self.risk,
                "reason": self.reason, "tool": self.tool, "source": self.source}


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
    except Exception:
        command_catalog = None
    if command_catalog is not None:
        try:
            command = command_catalog.by_name("/" + name)
        except command_catalog.CatalogUnavailable:
            # The classifier itself is blind. Continuing would report every
            # ``dangerous`` tool as ``ask``, which a caller with nobody to ask
            # resolves to ``allow`` -- a gate that cannot see refusing nothing.
            # Fail closed on ignorance instead; ``dangerous`` is the one class
            # that stops in every mode.
            return "dangerous"
        except Exception:
            command = None
        if command is not None:
            catalogued = command.risk
    if catalogued == "dangerous":
        return "dangerous"
    if name in EXECUTION_TOOLS:
        return "execution"
    # Unknown tool: treat as needing a prompt, never as safe.
    return catalogued or "ask"


def _default_rule_lookup(tool_name: str) -> dict | None:
    """The production rule source: the real on-disk ``permission_rules`` policy.

    This is the module-level hook ``decide()`` falls back on when no
    ``rule_lookup=`` is passed explicitly. It is deliberately a swappable
    attribute rather than code inlined into ``decide()``: it is the one part
    of the decision that touches the filesystem, and tests monkeypatch
    ``permission_modes._rule_lookup`` so ``decide()`` never has to read a
    real, machine-specific ``permissions.json`` to be exercised.

    Returns ``None`` when nothing genuinely matched. ``permission_rules.check``
    always returns *some* rule dict -- it falls back to a permissive wildcard
    "no matching rule" entry -- and that fallback must read as "no rule" here,
    not as an explicit ask that could be confused for one.
    """
    try:
        import sonder_paths
        import permission_rules
        from sonder_runtime.domain.execution import policy as _policy

        home = sonder_paths.default_home()
        rule = permission_rules.check(home, tool_name)
    except Exception:
        return None
    if not isinstance(rule, dict):
        return None
    if rule == dict(_policy.NO_MATCH_RULE):
        return None
    return rule


# Swappable so tests never have to touch a real, machine-specific
# permissions.json to exercise decide()'s rule-combination logic. Production
# code (and any test that wants the real thing) can restore this to
# ``_default_rule_lookup``, or pass ``rule_lookup=`` to a single ``decide()``
# call instead.
_rule_lookup = _default_rule_lookup


def _rule_action_for(tool_name: str, rule_lookup) -> tuple[str | None, str]:
    """Resolve ``(action, pattern)`` for a tool via a rule lookup, or (None, "").

    Only ``allow``/``deny`` are meaningful here -- see the module docstring.
    A missing/failing/malformed lookup, or a rule that says ``ask`` (or
    anything not recognised), is treated as "no rule": the mode alone decides.
    """
    lookup = rule_lookup if rule_lookup is not None else _rule_lookup
    if lookup is None:
        return None, ""
    try:
        rule = lookup(tool_name)
    except Exception:
        return None, ""
    if not isinstance(rule, dict):
        return None, ""
    action = str(rule.get("action", "")).strip().lower()
    if action not in (ALLOW, DENY):
        return None, ""
    return action, str(rule.get("pattern", "")).strip()


def decide(tool_name: str, *, interactive: bool = True,
           mode: str | None = None, rule_lookup=None,
           requires_elevation: bool = False) -> Decision:
    """Whether ``tool_name`` may run right now.

    ``interactive=False`` means nobody is present to answer a prompt (a direct
    MCP call). ``ask`` then degrades to ``allow`` -- preserving how Sonder has
    always behaved -- except under ``plan``, which denies regardless because
    holding still is the entire point of that mode.

    ``rule_lookup``, if given, overrides the module-level ``_rule_lookup``
    hook for this call only. See the module docstring for the precedence
    rules that combine a per-tool rule with the active mode.

    ``requires_elevation``, if given, flags THIS invocation -- not the tool in
    general -- as needing administrator rights, the same way a member of
    ``PRIVILEGED_TOOLS`` would. It exists because privilege in Sonder is
    usually a property of what a caller asks a general tool to do (see "Why
    PRIVILEGED_TOOLS is empty" in the module docstring), not a fixed property
    of the tool; a caller that already knows this particular call needs
    administrator rights can say so without every future call to the same
    tool being refused too.
    """
    active = mode or current_mode()
    if active not in _MATRIX:
        # Report the mode actually applied. Echoing an unknown name back in the
        # Decision put a mode that was never in effect into the audit trail.
        active = DEFAULT_MODE
    risk = risk_of(tool_name)
    name = str(tool_name or "").lstrip("/")

    rule_action, rule_pattern = _rule_action_for(name, rule_lookup)

    # 1. An explicit deny is a narrower, written-down decision than any mode
    #    dial, so it wins outright -- including over auto, and immune to the
    #    non-interactive degrade below (a real deny is never softened).
    if rule_action == DENY:
        return Decision(
            DENY, active, risk,
            "rule denies this tool (pattern %r); an explicit deny outranks "
            "every mode, including auto" % (rule_pattern or name),
            name,
            source="rule",
        )

    # 2. Privilege is a separate axis from both modes and rules; neither can
    #    grant it. A tool can need it unconditionally (PRIVILEGED_TOOLS,
    #    currently empty by design) or only for this one call
    #    (requires_elevation, set by the caller).
    if (name in PRIVILEGED_TOOLS or requires_elevation) and not elevated():
        return Decision(DENY, active, risk, "needs elevation, which is off", name,
                        source="privilege")

    mode_action = _MATRIX[active].get(risk, ASK)

    # 3. An explicit allow loosens an ask into an allow -- but only an ask:
    #    it must not appear to have decided anything when the mode already
    #    allowed the tool, and (4) plan's denials are never a mode's "ask" in
    #    the first place, so this branch naturally never fires under plan.
    if rule_action == ALLOW and mode_action == ASK:
        return Decision(
            ALLOW, active, risk,
            "rule allows this tool (pattern %r), satisfying %s's ask"
            % (rule_pattern or name, MODE_LABELS.get(active, active)),
            name,
            source="rule",
        )

    # 5. No rule applied (or it was inert): the mode alone decides, exactly as
    #    it did before rules were wired in.
    action = mode_action
    if action == ASK and not interactive:
        return Decision(
            ALLOW, active, risk,
            "no interactive prompt available; %s tools are not blocked outside "
            "the console" % risk,
            name,
            source="non-interactive",
        )
    reasons = {
        ALLOW: "%s allows %s tools" % (MODE_LABELS.get(active, active), risk),
        ASK: "%s asks before %s tools" % (MODE_LABELS.get(active, active), risk),
        DENY: "%s forbids %s tools" % (MODE_LABELS.get(active, active), risk),
    }
    return Decision(action, active, risk, reasons[action], name, source="mode")


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
    if ASK in _MATRIX[active].values():
        # The rows above are the raw matrix, and an `ask` row read alone is a
        # promise this mode only keeps for a caller somebody can answer for.
        lines += ["", "  %s" % ASK_CAVEAT]
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
        "  %s" % ASK_CAVEAT,
    ]
    if elevated():
        lines.append("  PRIVILEGE: elevated%s" % (
            " - " + elevation_reason() if elevation_reason() else ""))
    return "\n".join(lines)
