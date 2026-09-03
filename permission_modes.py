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

Read that as the promise it is: ``ask`` is a promise about a *prompt*, and a
prompt needs somebody to answer it. Every protocol surface below passes
``interactive=False`` -- the agent, loop and control paths, both MCP entry
points, the HTTP chain, and a piped console -- and there ``ask`` fails closed
for anything that changes files, runs a host program, or is graded
``dangerous`` (``UNATTENDED_REFUSED_RISKS``): the call is refused with the
remedies named, and the refusal is recorded (see "Unattended callers"). So
``auto`` plus no operator never runs a ``dangerous`` tool; it is refused until
an operator writes an explicit ``allow`` rule or answers at the console.
``plan`` denies everywhere. ``ASK_CAVEAT`` carries the exact rule onto every
surface that repeats the claim.

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
    sonder_serve._http_tool_refusal the app's slash chain and its catalogued
                                    fall-through. ``interactive=False``.
    native_mcp                      the typed MCP transport's host-control
                                    tools. ``interactive=False``.

The console's two entries are disjoint by construction: a typed command is
served by a named branch or by the catalogued fallback, never both, so nothing
is prompted for twice.

``interactive`` means "somebody is present to answer", which is why the console
sites compute it (``sonder_repl._console_has_operator``) rather than assert it.
`sonder < script.txt` is a console session with nobody at the keyboard, and
there ``input()`` does not ask anyone anything -- it consumes the next line of
the script. A piped console therefore degrades exactly like a protocol caller.

Interactive surfaces honour ``ask`` by actually asking (the console prompts
``y/N``, defaulting to no).

Unattended callers
------------------
A caller with no one to ask gets one of three answers for a mode's ``ask``:

    mutation / execution / dangerous   refused (``source="unattended"``).
                                       Remedies, named in the refusal: an
                                       explicit ``allow`` rule, a mode that
                                       already allows the class (``acceptEdits``
                                       for file changes, ``auto`` for host
                                       programs; nothing allows ``dangerous``
                                       unattended), the console, or a one-shot
                                       approval of exactly this call (below).
    ask                                proceeds, recorded
                                       (``source="non-interactive"``). This is
                                       the class the catalog gives the chat,
                                       task and memory entry points
                                       (``sonder``, ``agent``, ``task_create``);
                                       refusing it would refuse every
                                       conversation over MCP and HTTP, while
                                       the effects those entry points can
                                       cause are gated tool by tool on the
                                       agent path.
    unclassified / durable authority   refused, as before (5a and 5b below).

``plan`` denies everything that is not a read whoever is present.

One-shot approvals
------------------
A caller that passes its ``arguments`` gives the gate a *call digest*
(``call_digest``: the tool name plus the canonical JSON of the arguments with
the credential knobs removed). When such a call is refused unattended for an
effect class, the refusal carries a call id (the digest's first 16 hex
characters) and the call is noted as pending in the approval ledger
(``sonder_runtime.adapters.security.approval_ledger``). An operator at the
console approves exactly that call once -- ``/approve <call id>``, the
``permission_approve`` tool -- and the next unchanged call, from any surface,
consumes the approval and runs (``source="approval"``). The approval is one
tool and one digest, spent atomically on first use, and expires; it is not a
rule, and it never touches ``plan``'s denials, an explicit ``deny`` rule, the
unclassified grade or the durable-authority class. A preflight
(``record=False``) never spends one.

A spent approval also carries the *reach* the operator approved: the digest
binds the call's ``extra_roots``, so the surface that spent it may honour
exactly those roots for exactly that call (``approval_spent_for``, consulted
by the filesystem adapter's reach scope). That is the only way a call gains
roots it was not configured with, now that the shared
``SONDER_FILE_APPROVAL_CODE`` is retired: a static secret in a model-visible
argument that switched containment off entirely.

Effect fences
-------------
A worker that holds a lease -- the autopilot controller -- installs an effect
fence for the duration of a task (``sonder_runtime.adapters.execution.
effect_fence``). ``decide()`` consults the fence before deciding any tool of
an effect class and refuses (``source="fence"``) the moment the lease is lost
or the run cancelled, whatever the mode or rules say; reads are never fenced.

Every unattended decision that is a refusal, or an allow of anything but a
``safe`` read, is handed to the decision observers the composition root
installs (``add_decision_observer``). The production observer
(``sonder_runtime.adapters.security.permission_receipts``) writes a
content-free receipt -- tool, surface, mode, risk, source, action -- to the
operations event store, and ``unattended_summary()`` shows the running
counts on ``/permissions``. A gate that refuses silently is one operators
route around; a gate that allows silently is one nobody can audit.

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

    0. a lost effect fence     refuses every effect-class call on the fenced
                                 thread before any policy is read (see
                                 "Effect fences" above); reads pass.
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
                                 what it decided before rules were wired in --
                                 except that an unattended ``ask`` for an
                                 effect class first looks for a one-shot
                                 approval of exactly this call (5c) and spends
                                 it if one is open.

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
in one word (``rule``/``mode``/``privilege``/``unattended``/``non-interactive``)
for anything
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

import contextvars
import hashlib
import json
import logging
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
# prompt; with nobody to prompt, the effect classes are refused and the ask
# class proceeds on the record. `server._permission_mode_context` prints this
# on every `/permissions` render and the presentation functions below repeat
# it, so a reader of any of them can get from a row to their own answer.
ASK_CAVEAT = (
    "'ask' means a prompt at the console; with nobody to ask, file changes, "
    "host programs, and destructive tools are refused (write an allow rule, "
    "pick a mode that allows the class, or answer at the console), while "
    "ask-class tools proceed and are recorded; plan denies, and tools nothing "
    "can classify are always refused."
)

MODE_BLURBS = {
    PLAN: "reads only - no writes, no commands",
    MANUAL: "ask before anything that is not a read; refused when nobody can be asked",
    ACCEPT_EDITS: "file changes proceed; running programs asks, or is refused unattended",
    # This string has the widest reach of any of them:
    # `server.permission_mode_data()` ships it to the Flutter client, which
    # renders it on the mode chip and in the mode picker. Name where the prompt
    # happens and what happens when it cannot, because both are true and only
    # one used to be said.
    AUTO: "file changes and programs proceed; destructive asks at the console, or is refused unattended",
}

# Colour hints for whatever renders the indicator (ANSI 256 palette).
MODE_COLOURS = {PLAN: 117, MANUAL: 250, ACCEPT_EDITS: 114, AUTO: 221}

# The grade ``risk_of`` returns when it CANNOT grade: an empty name, a blind
# catalog, or a name nothing has ever heard of. Deliberately not one of the
# severity classes, because severity cannot express this.
#
# Every production gate calls ``decide(interactive=False)`` --
# ``reloadable_mcp._refuse_if_gated``, ``server._agent_permission_gate_error``,
# ``server._loop_permission_refusal``, ``server._control_tool_refusal``,
# ``sonder_serve._http_tool_refusal`` -- and that path once degraded ASK to
# ALLOW for every grade. The effect classes now fail closed there
# (``UNATTENDED_REFUSED_RISKS``), but the ``ask`` class still proceeds, so a
# name graded ``ask`` by accident would still run unattended. Returning a
# scarier class was the previous attempt at failing closed and it achieved
# nothing; what is needed is a class no degrade applies to.
UNCLASSIFIED = "unclassified"

# Work reachable through a gate that legitimately fronts no registered tool,
# with the grade it would carry if it did. Without this, ``sleep`` -- a clamped
# ``time.sleep`` in ``server._loop_dispatch`` -- lands on the unknown-name path
# and the change above would refuse it: the classifier denying service to the
# runtime, which is the failure a fail-closed change has to avoid. Declaring it
# makes the exception visible and reviewable instead of silent, exactly as
# ``EXECUTION_COMMANDS`` declares ``/runwindow``. A drift test pins this to the
# loop actions that really run no tool, so it cannot become a list of strings.
NON_TOOL_WORK = {"sleep": "safe"}

# Host-control tools registered by the native MCP transport rather than the
# legacy server MCP registry. Keeping the two compute mutations explicit here
# makes unattended policy fail by deliberate class instead of "unclassified".
NATIVE_MCP_WORK = {
    "compute_submit": "execution",
    "compute_cancel": "mutation",
}

# Risk classes an unattended caller is refused for when the mode says ``ask``.
# ``safe`` never reaches the ask branch; ``ask`` proceeds on the record (see
# "Unattended callers" above); ``unclassified`` is refused by its own branch.
UNATTENDED_REFUSED_RISKS = frozenset({"mutation", "execution", "dangerous"})

# Argument names that carry authority rather than describing the call. They
# are removed before a call is digested, so the call an operator approved and
# the call a surface retries hash the same whatever token, approval string or
# host-injected knob travelled with them -- and so that none of them is ever
# previewed.
CREDENTIAL_ARGUMENTS = frozenset({"token", "approval", "bypass", "developer_authorized"})

# Bulk payloads: digested in full (an approval is for exactly this content)
# but never previewed; ``argument_preview`` shows their length instead.
BULK_ARGUMENTS = frozenset({
    "content", "patch", "operations", "operations_json", "old", "new", "text",
    "prompt", "code", "stdin", "args_json", "inputs_json",
})

CALL_ID_CHARS = 16


def _call_body(arguments) -> dict | None:
    if arguments is None:
        return None
    try:
        items = dict(arguments)
    except (TypeError, ValueError):
        return None
    return {
        str(key): value for key, value in items.items()
        if str(key) not in CREDENTIAL_ARGUMENTS
    }


def call_digest(tool_name: str, arguments) -> str:
    """SHA-256 over the tool name and its canonical, credential-free arguments.

    "" when there is nothing to digest (no arguments, or arguments that have no
    canonical JSON form): such a call can neither be approved nor pending.
    """
    name = str(tool_name or "").strip().lstrip("/")
    body = _call_body(arguments)
    if not name or body is None:
        return ""
    try:
        canonical = json.dumps(
            body, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=True,
        )
    except (TypeError, ValueError):
        return ""
    return hashlib.sha256(("%s\n%s" % (name, canonical)).encode("utf-8")).hexdigest()


def call_id(digest: str) -> str:
    """The short, typeable form of a call digest."""
    return str(digest or "")[:CALL_ID_CHARS]


def argument_preview(arguments, limit: int = 200) -> str:
    """A bounded, content-free line naming what a call was about.

    Keys and short scalar values, credential knobs omitted, bulk payloads
    shown only by their length. This is what ``/approvals`` shows an operator
    next to a call id; it identifies the call without reproducing it.
    """
    body = _call_body(arguments)
    if not body:
        return ""
    parts = []
    for key in sorted(body):
        value = body[key]
        if key in BULK_ARGUMENTS:
            try:
                size = len(value) if isinstance(value, (str, bytes, list, tuple, dict)) else len(str(value))
            except Exception:
                size = 0
            parts.append("%s=<%d chars>" % (key, size) if isinstance(value, str)
                         else "%s=<%d items>" % (key, size))
            continue
        if isinstance(value, str):
            text = value
        else:
            try:
                text = json.dumps(value, sort_keys=True, default=str, ensure_ascii=True)
            except (TypeError, ValueError):
                text = str(value)
        text = text.replace("\r", " ").replace("\n", " ")
        if len(text) > 40:
            text = text[:37] + "..."
        parts.append("%s=%s" % (key, text))
    line = " ".join(parts)
    return line if len(line) <= limit else line[:limit - 3] + "..."

# Read-only branches of a slash command that fronts a dangerous tool, graded
# because they were declared. ``/selfmod status`` reads
# ``selfmod.format_status`` and calls no registered tool, but the chain gates
# grade ``/selfmod`` by its strictest member, which would refuse the read for
# an unattended caller. ``command_catalog.narrow_branch_tools`` substitutes
# these names for the read forms it recognises from the argument grammar, and
# a drift test pins each entry to a narrowing rule that actually produces it,
# so this cannot become a list of free-floating exemptions.
READ_BRANCH_WORK = {
    "selfmod_status": "safe",   # /selfmod [status|show|list|history|inspect|diff|tests|backups]
    "goal_status": "safe",      # /goal [show|status|history|proposals]
    "training_status": "safe",  # /training [plan|status|hardware|help]
}

# risk class -> action, per mode. "execution" is a synthetic class: tools that
# start a host process, split out of `ask`/`mutation` so acceptEdits and auto
# differ by something real.
#
# ``unclassified`` reads as ASK outside ``plan`` so a person at a console is
# *asked* rather than refused, but ``decide()`` refuses to degrade it for a
# caller with nobody to ask. An operator who wants such a name to run says so
# with an explicit ``allow`` rule, which still satisfies the ASK below; that is
# the escape hatch that keeps failing closed from becoming a denial of service.
_MATRIX = {
    PLAN: {"safe": ALLOW, "ask": DENY, "mutation": DENY,
           "execution": DENY, "dangerous": DENY, UNCLASSIFIED: DENY},
    MANUAL: {"safe": ALLOW, "ask": ASK, "mutation": ASK,
             "execution": ASK, "dangerous": ASK, UNCLASSIFIED: ASK},
    ACCEPT_EDITS: {"safe": ALLOW, "ask": ALLOW, "mutation": ALLOW,
                   "execution": ASK, "dangerous": ASK, UNCLASSIFIED: ASK},
    AUTO: {"safe": ALLOW, "ask": ALLOW, "mutation": ALLOW,
           "execution": ALLOW, "dangerous": ASK, UNCLASSIFIED: ASK},
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
    "self_heal_repair", "scaffold_project", "compiler_cache_status",
})

# The same class, for work that no *registered tool* fronts. ``EXECUTION_TOOLS``
# above is pinned by a drift test to names the MCP registry actually knows, and
# that invariant is worth keeping -- but a console branch can start a host
# process without going through a tool at all. ``/runwindow`` calls
# ``code_runner.run_code_window`` directly, so it resolved to nothing, and a
# gate handed an empty set allows: ``/run`` was refused under ``plan`` while
# ``/runwindow`` launched the same code block in a detached console.
#
# Entries here are the stand-in names ``command_catalog._UNREGISTERED_BRANCH_WORK``
# maps such branches to, and a drift test requires that correspondence in both
# directions, so this cannot drift into a free-floating list of strings.
EXECUTION_COMMANDS = frozenset({"runwindow"})

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

# Tools that hand out authority which outlives the call. The non-interactive
# degrade does not apply to these -- see ``decide()`` step 5b.
#
# Task #18 made ``UNCLASSIFIED`` non-degradable and left ``dangerous``
# degradable, and for an elevation primitive that ordering is inverted: the
# grade for "we do not know what this is" refuses to relax while the grade for
# "this is the most dangerous thing we have" still does. Measured with no
# operator rules, every one of these came back ALLOW in manual, acceptEdits and
# auto with ``interactive=False``, which is the only path the five production
# gates use.
#
# Why this class and not the whole ``dangerous`` grade. The catalog grades 19
# commands ``dangerous``; 8 of them are reachable from
# ``server._agent_dispatch`` (``file_delete``, ``git_cherry_pick``,
# ``git_merge``, ``memory_privacy_repair``, ``memory_quality_repair``,
# ``self_heal_repair``, ``sqlite_mutate``, ``task_delete``). A class-wide
# non-degrade would refuse all eight in every non-interactive lane -- the agent
# and autopilot lanes entirely -- which is a shutdown, not a gate. So the line
# is drawn where the degrade's own justification stops holding rather than at
# the severity label.
#
# That justification is a trade: accept an unanswerable prompt resolving to
# yes, because the result can be undone afterwards. For privilege the "undo" is
# "the operator can revoke it later", which assumes the operator KNOWS IT
# HAPPENED -- and a degraded prompt is precisely the notification that did not
# occur. Granting an account, assuming one, logging in, switching elevation on,
# or writing a standing permission rule all leave state behind and no prompt
# behind. None of the five is dispatchable by the agent or loop paths
# (measured against ``tool_capabilities.dispatch_names``), so this costs those
# lanes nothing; the surfaces it binds are the MCP protocol entry point and the
# HTTP one, where "nobody is present" is literally true.
#
# ``permission_rule_set`` is a member because without it the class does not
# bind. ``decide()`` resolves an explicit ALLOW rule at step 3, BEFORE the
# degrade, so a caller who can write rules unattended writes one for
# ``admin_register`` and walks through. Bootstrapping authority has to cost a
# person's attention or it is not a boundary.
#
# Both routes out survive and are deliberate: a console operator arrives with
# ``interactive=True`` and is asked, and an explicit ``allow`` rule -- narrow,
# persistent, auditable -- still satisfies the ask at step 3. A refusal with no
# route out is one operators learn to route around.
#
# Not extended to ``/selfmod``: ``server._selfmod_command`` gates the two
# source-writing actions on the ACTION (``_SELFMOD_SOURCE_WRITING_ACTIONS``),
# because ``/selfmod status`` and ``/selfmod deploy`` arrive at the same
# command and refusing a status read unattended is the over-refusal these
# gates exist to avoid. That is the right shape there and the wrong shape
# here; the two mechanisms compose, and this list must not grow to swallow
# that one.
#
# ``permission_approve`` is a member for the same reason ``permission_rule_set``
# is: it issues authority that outlives the call (a one-shot approval another
# caller spends later), so a caller that could approve unattended would
# approve its own next call. The console operator answering the prompt is the
# route out, exactly as for the rest of the class.
DURABLE_AUTHORITY_TOOLS = frozenset({
    "admin_login", "admin_register", "admin_set_account",
    "elevate", "permission_rule_set", "permission_approve",
})

_LOCK = threading.RLock()
_STATE = {"mode": DEFAULT_MODE, "elevated": False, "elevation_reason": ""}
_LOADED = False

# --- unattended decision receipts ----------------------------------------
#
# ``decide()`` is pure and must not open a database. Observers are installed
# by the composition root (``sonder_runtime.adapters.security.
# permission_receipts``) and receive every unattended decision worth a
# receipt: a refusal, or an allow of anything but a ``safe`` read. The
# counters below are process-local and back the ``/permissions`` summary
# line; the durable record is the observer's. An observer that raises is
# dropped for that call and never changes the decision.
_DECISION_OBSERVERS: list = []
_UNATTENDED_LOCK = threading.Lock()
_UNATTENDED = {"refused": 0, "allowed": 0, "last_refusal": "", "last_allow": ""}
_FIRST_REFUSAL_HINT = (
    "first unattended permission refusal in this process: /permissions shows "
    "which rule or mode governs each tool; /mode acceptEdits lets file changes "
    "proceed unattended and /mode auto lets host programs proceed too"
)
_HINT_STATE = {"shown": False}
_log = logging.getLogger(__name__)


def add_decision_observer(observer) -> None:
    """Register ``observer(decision, surface)`` for unattended decisions."""
    if not callable(observer):
        raise TypeError("a decision observer must be callable")
    with _UNATTENDED_LOCK:
        if observer not in _DECISION_OBSERVERS:
            _DECISION_OBSERVERS.append(observer)


def remove_decision_observer(observer) -> None:
    with _UNATTENDED_LOCK:
        if observer in _DECISION_OBSERVERS:
            _DECISION_OBSERVERS.remove(observer)


def _worth_a_receipt(decision) -> bool:
    """A refusal, or an allow of anything that is not a read."""
    return decision.action == DENY or decision.risk != "safe"


def _observe(decision, surface: str) -> None:
    label = "%s via %s" % (decision.tool or "(empty name)", surface or "unspecified")
    with _UNATTENDED_LOCK:
        if decision.action == DENY:
            _UNATTENDED["refused"] += 1
            _UNATTENDED["last_refusal"] = label
        else:
            _UNATTENDED["allowed"] += 1
            _UNATTENDED["last_allow"] = label
        observers = list(_DECISION_OBSERVERS)
    for observer in observers:
        try:
            observer(decision, surface or "unspecified")
        except Exception:
            # The receipt is evidence, not authority: a broken sink must
            # neither block nor change the decision it was told about.
            continue


def _note_first_refusal() -> None:
    with _UNATTENDED_LOCK:
        if _HINT_STATE["shown"]:
            return
        _HINT_STATE["shown"] = True
    _log.warning(_FIRST_REFUSAL_HINT)


def unattended_summary() -> str:
    """One line for ``/permissions``: what unattended callers got since start."""
    with _UNATTENDED_LOCK:
        refused, allowed = _UNATTENDED["refused"], _UNATTENDED["allowed"]
        last_refusal, last_allow = _UNATTENDED["last_refusal"], _UNATTENDED["last_allow"]
    parts = ["unattended decisions since start: %d refused, %d allowed" % (refused, allowed)]
    if last_refusal:
        parts.append("last refusal: %s" % last_refusal)
    if last_allow:
        parts.append("last allow: %s" % last_allow)
    return "; ".join(parts)


def reset_unattended_for_tests() -> None:
    with _UNATTENDED_LOCK:
        _UNATTENDED.update({"refused": 0, "allowed": 0, "last_refusal": "", "last_allow": ""})
        _HINT_STATE["shown"] = False


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
    # rule | mode | privilege | unattended | non-interactive | unclassified |
    # durable-authority | approval | fence -- the layer that decided.
    # Defaulted to the commonest case so the field cannot be forgotten into a
    # crash, but every return site in decide() sets it explicitly.
    source: str = "mode"
    # The short digest of the call this decision is about, when the caller
    # passed arguments (see "One-shot approvals"); "" otherwise. A digest, not
    # an argument: the receipt stays content-free.
    call_id: str = ""

    @property
    def allowed(self) -> bool:
        return self.action == ALLOW

    def to_dict(self) -> dict:
        return {"action": self.action, "mode": self.mode, "risk": self.risk,
                "reason": self.reason, "tool": self.tool, "source": self.source,
                "call_id": self.call_id}


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
        # promising the opposite -- and so, it turned out, did returning
        # "ask": ``decide(interactive=False)`` allows that, so the comment
        # stayed false for a second reason. UNCLASSIFIED is the grade that
        # actually keeps the promise.
        return UNCLASSIFIED
    catalogued = ""
    try:
        import command_catalog
    except Exception:
        # The classifier is blind -- see the blind-catalog note below. This
        # used to set ``command_catalog = None`` and skip the lookup block,
        # which left ``catalogued`` empty and fell through to the static
        # tables at the bottom, so a partially-initialised server (the case
        # where an import fails) got a confident grade from a table instead
        # of the refusal the branch below returns for the same condition.
        return UNCLASSIFIED
    try:
        command = command_catalog.by_name("/" + name)
    except Exception:
        # The classifier itself is blind. Continuing would report every
        # ``dangerous`` tool as ``ask``, which a caller with nobody to ask
        # resolves to ``allow`` -- a gate that cannot see refusing nothing.
        #
        # This used to return "dangerous", on the stated grounds that it
        # "is the one class that stops in every mode". Measured, that is
        # false on the only path this matters: ``dangerous`` is ASK in
        # manual, acceptEdits and auto, and ``interactive=False`` degrades
        # ASK to ALLOW. The blind-catalog branch therefore allowed
        # git_merge in three of four modes. UNCLASSIFIED is not degraded,
        # so the branch now does what it always said it did.
        #
        # This catches ``Exception``, not just ``CatalogUnavailable``, and the
        # width is the point. ``catalog()`` converts only the registry read
        # into ``CatalogUnavailable``; ``import command_registry``,
        # ``_native_groups()``, ``Command`` construction and ``_category_for``
        # are unwrapped, so the partially-initialised server that
        # ``CatalogUnavailable``'s docstring cites usually arrives as an
        # ``ImportError``/``AttributeError`` instead. A narrower
        # ``except CatalogUnavailable`` beside a broad ``except Exception:
        # command = None`` meant those fell through to the static tables
        # below -- and those tables can answer SOFTER than the catalog:
        # ``self_heal_repair`` is catalog-``dangerous`` and in
        # ``EXECUTION_TOOLS``, so blind it graded ``execution``, which ``auto``
        # ALLOWS outright. The gate cannot know WHY it went blind, only that
        # it did, so every way of going blind must grade the same.
        return UNCLASSIFIED
    if command is not None:
        catalogued = command.risk
    if catalogued == "dangerous":
        return "dangerous"
    if name in EXECUTION_TOOLS or name in EXECUTION_COMMANDS:
        return "execution"
    if catalogued:
        return catalogued
    if name in NATIVE_MCP_WORK:
        return NATIVE_MCP_WORK[name]
    # Work that fronts no registered tool, graded because it was declared.
    # Checked after the catalog so a real tool of the same name always wins.
    if name in NON_TOOL_WORK:
        return NON_TOOL_WORK[name]
    if name in READ_BRANCH_WORK:
        return READ_BRANCH_WORK[name]
    # Nothing knows this name. Not "probably fine, ask someone" -- that
    # answer was indistinguishable from a catalogued ``ask`` and, with nobody
    # to ask, indistinguishable from ``allow``. Say the classifier failed.
    return UNCLASSIFIED


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
        rule, report = permission_rules.check_report(home, tool_name)
    except Exception:
        return None
    # A corrupt, unreadable, or partially accepted policy must not silently
    # turn an operator's explicit deny into an unattended allow.  The normal
    # first-run absence is deliberately healthy (``report.degraded`` is
    # false), so local defaults continue to work as before.  Read-only status
    # tools stay available during recovery; every other class is refused until
    # the artifact is repaired rather than inheriting a permissive default.
    if report.degraded and risk_of(tool_name) != "safe":
        return {
            "pattern": "<degraded permission policy>",
            "action": DENY,
            "note": "permission policy could not be enforced safely",
        }
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


def _default_approval_ledger():
    """The production one-shot approval ledger, resolved lazily (it is an adapter)."""
    from sonder_runtime.adapters.security import approval_ledger

    return approval_ledger.default_ledger()


# Swappable for the same reason ``_rule_lookup`` is: a test of the decision
# logic must not need a real approvals database. Returns the ledger to consult
# (anything with ``consume`` and ``record_pending``), or None to consult none.
_approval_ledger = _default_approval_ledger

# The approval the most recent live decision on this context spent, as
# ``(tool, digest)``. It exists so the surface that made the decision can
# honour the reach the operator approved for exactly that call; it is
# replaced by the next live decision on the same context and cleared by the
# surface when the call is over (``forget_spent_approval``). Per context, so
# two concurrent protocol calls never see each other's.
_SPENT_APPROVAL: contextvars.ContextVar = contextvars.ContextVar(
    "sonder_spent_approval", default=None,
)


def approval_spent_for(tool_name: str, arguments) -> bool:
    """Whether the last live decision on this context spent an approval for exactly this call."""
    spent = _SPENT_APPROVAL.get()
    if spent is None:
        return False
    name = str(tool_name or "").strip().lstrip("/")
    digest = call_digest(name, arguments)
    return bool(digest) and spent == (name, digest)


def forget_spent_approval() -> None:
    """Clear the spent-approval note once the call it was for is over."""
    _SPENT_APPROVAL.set(None)


def approval_ledger():
    """The one-shot approval ledger the gate consults, or None when there is none.

    The same hook ``decide()`` resolves, so the ``permission_approve`` tool
    and ``/approvals`` issue into and list from exactly the ledger the gate
    spends from -- in production and under a test that swapped the hook.
    """
    return _ledger_for(None)


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


def decide_for_caller(tool_name: str, *, interactive: bool,
                      gate_control_exempt: bool, surface: str = "",
                      record: bool = True, mode: str | None = None,
                      rule_lookup=None, arguments=None, fence=None):
    """``decide()`` plus the one exemption, for callers that share both.

    Returns ``None`` when the tool is exempt and there is therefore nothing to
    decide; otherwise the ``Decision``.

    ``mode`` and ``rule_lookup`` are the same per-call overrides ``decide()``
    takes; the evaluation lane passes them so a ``tool_policy`` scenario can
    ask what this kind of caller would get under a stated mode and rule set
    without touching the operator's own. ``arguments`` and ``fence`` are
    passed through as well: the first lets a one-shot approval of exactly
    this call answer an unattended ask, the second lets a worker's lost
    lease refuse the effect (see ``decide()``).

    The exemption existed as six identical lines at four call sites, and this
    round it drifted at the fifth: the gate added to
    ``server.control_command``'s catalogued fall-through consulted ``decide()``
    without it, so ``permission_mode`` -- risk ``ask``, which ``plan`` denies
    -- came back ``deny`` on that path. A set that must be consulted at every
    person-facing surface, by hand, is a set that will be missed at the next
    surface someone adds; so the surfaces now ask for a decision *for a kind of
    caller* and this function owns which exemptions that kind carries.

    ``gate_control_exempt`` is the caller kind, and it is the whole distinction
    the module docstring draws: a person driving Sonder (console, MCP client,
    app) must keep a way out of ``plan``; Sonder driving itself (the agent and
    loop paths) must not be able to lift its own restraint. It is a required
    keyword because there is no defensible default -- getting it wrong in
    either direction is a security answer, not a convenience.
    """
    if gate_control_exempt and str(tool_name or "").strip().lstrip("/") in GATE_CONTROL_TOOLS:
        return None
    return decide(tool_name, interactive=interactive, mode=mode,
                  rule_lookup=rule_lookup, surface=surface, record=record,
                  arguments=arguments, fence=fence)


def decide(tool_name: str, *, interactive: bool = True,
           mode: str | None = None, rule_lookup=None,
           requires_elevation: bool = False,
           surface: str = "", record: bool = True,
           arguments=None, fence=None, approval_ledger=None) -> Decision:
    """Whether ``tool_name`` may run right now.

    ``interactive=False`` means nobody is present to answer a prompt (a direct
    MCP call, the HTTP chain, the agent and loop paths, a piped console). A
    mode's ``ask`` is then answered by the class of the tool: file changes,
    host programs and destructive tools are refused (``source="unattended"``)
    and the refusal names the remedies; ``ask``-class tools proceed and are
    recorded (``source="non-interactive"``); ``plan`` denies regardless. See
    "Unattended callers" in the module docstring for why the line sits there.
    Both routes out survive: an explicit ``allow`` rule is resolved at (3)
    below, before this ever runs, and a console operator who answers the
    prompt reaches here with ``interactive`` already true.

    ``surface`` names the entry point for the receipt an unattended decision
    leaves (``agent``, ``loop``, ``control``, ``mcp``, ``native-mcp``,
    ``http``, ``repl``); it never changes the decision. ``record=False`` is
    for preflight callers (``policy_explain``) that decide without acting.

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

    ``arguments``, if given, are the call's own arguments (a mapping). They
    are never stored or logged: they are digested (``call_digest``) so that an
    unattended refusal of an effect class can name the call (``Decision.
    call_id``), note it as pending, and -- on the next unchanged call -- be
    answered by a one-shot approval an operator issued for exactly it. Both
    the noting and the spending happen only when ``record`` is true: a
    preflight neither burns an approval nor leaves a request behind.

    ``fence``, if given, is an effect fence (``effect_fence.Fence``, or any
    callable returning "" while it holds) consulted before every effect-class
    decision; a fence that no longer holds refuses with ``source="fence"``.

    ``approval_ledger`` overrides the module-level ``_approval_ledger`` hook
    for this call, the way ``rule_lookup`` overrides ``_rule_lookup``.
    """
    decision = _decide(
        tool_name, interactive=interactive, mode=mode, rule_lookup=rule_lookup,
        requires_elevation=requires_elevation, arguments=arguments, fence=fence,
        approval_ledger=approval_ledger, surface=surface, live=record,
    )
    if record and not interactive and _worth_a_receipt(decision):
        _observe(decision, surface)
    return decision


_EFFECT_NOUNS = {
    "mutation": "changes files",
    "execution": "runs a host program",
    "dangerous": "is destructive or administrative",
}


def _modes_allowing(risk: str) -> list:
    """Modes whose matrix already allows ``risk`` outright, least autonomous first."""
    return [m for m in MODES if _MATRIX[m].get(risk) == ALLOW]


def _unattended_reason(name: str, active: str, risk: str, call: str = "") -> str:
    """Name what was refused and every route out, so the refusal can be acted on."""
    remedies = ["write an explicit allow rule with /permissions"]
    modes = [m for m in _modes_allowing(risk) if m != active]
    if modes:
        remedies.append("switch to %s" % " or ".join(modes))
    remedies.append("run it from the console and answer the prompt")
    if call:
        remedies.append(
            "approve exactly this call once at the console with /approve %s" % call
        )
    noun = _EFFECT_NOUNS.get(risk, "is graded %s" % risk)
    return (
        "%s %s and nobody is here to answer %s's ask, so it is refused "
        "rather than assumed; %s"
        % (name or "(empty name)", noun, MODE_LABELS.get(active, active),
           ", or ".join(remedies))
    )


def _fence_reason(fence) -> str:
    """"" while ``fence`` holds; otherwise why it does not (a failing check included)."""
    check = getattr(fence, "check", fence)
    label = getattr(fence, "label", "") or "effect fence"
    try:
        return str(check() or "")
    except Exception as exc:
        return "%s could not be verified: %s" % (label, exc)


def _ledger_for(approval_ledger):
    if approval_ledger is not None:
        return approval_ledger
    provider = _approval_ledger
    if provider is None:
        return None
    try:
        return provider()
    except Exception:
        # No ledger is "no approval can apply", never a crash of the gate.
        return None


def _decide(tool_name: str, *, interactive: bool, mode: str | None,
            rule_lookup, requires_elevation: bool, arguments=None, fence=None,
            approval_ledger=None, surface: str = "", live: bool = True) -> Decision:
    active = mode or current_mode()
    if active not in _MATRIX:
        # Report the mode actually applied. Echoing an unknown name back in the
        # Decision put a mode that was never in effect into the audit trail.
        active = DEFAULT_MODE
    risk = risk_of(tool_name)
    name = str(tool_name or "").lstrip("/")
    digest = call_digest(name, arguments)
    call = call_id(digest)
    if live:
        # A live decision starts a new call on this context; whatever the
        # previous one spent is not this one's.
        _SPENT_APPROVAL.set(None)

    # 0. The fence on this thread's effects, before any policy is read. A
    #    worker whose lease is gone produces no effect whatever the mode or
    #    the rules say; it may still read.
    if fence is not None and risk in UNATTENDED_REFUSED_RISKS:
        lost = _fence_reason(fence)
        if lost:
            return Decision(
                DENY, active, risk,
                "%s %s, and the fence on this worker's effects no longer holds: %s"
                % (name or "(empty name)", _EFFECT_NOUNS.get(risk, "is graded %s" % risk), lost),
                name, source="fence", call_id=call,
            )

    rule_action, rule_pattern = _rule_action_for(name, rule_lookup)

    # 1. An explicit deny is a narrower, written-down decision than any mode
    #    dial, so it wins outright -- including over auto, and immune to the
    #    unattended rule below (a real deny is never softened).
    if rule_action == DENY:
        return Decision(
            DENY, active, risk,
            "rule denies this tool (pattern %r); an explicit deny outranks "
            "every mode, including auto" % (rule_pattern or name),
            name,
            source="rule", call_id=call,
        )

    # 2. Privilege is a separate axis from both modes and rules; neither can
    #    grant it. A tool can need it unconditionally (PRIVILEGED_TOOLS,
    #    currently empty by design) or only for this one call
    #    (requires_elevation, set by the caller).
    if (name in PRIVILEGED_TOOLS or requires_elevation) and not elevated():
        return Decision(DENY, active, risk, "needs elevation, which is off", name,
                        source="privilege", call_id=call)

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
            source="rule", call_id=call,
        )

    # 5. No rule applied (or it was inert): the mode alone decides, exactly as
    #    it did before rules were wired in.
    action = mode_action
    if action == ASK and not interactive:
        # 5a. The one grade no rule below may touch. "Nobody is here to
        #     answer" is never a reason to proceed when the gate does not know
        #     what it is confirming. An explicit ``allow`` rule already
        #     resolved above (3), so an operator who wants this name to run
        #     has a written way to say so.
        if risk == UNCLASSIFIED:
            return Decision(
                DENY, active, risk,
                "nothing could classify %r, and there is no one to ask; "
                "refusing rather than downgrading an unclassified tool. Add a "
                "permission rule for it, or register it in the command catalog"
                % (name or "(empty name)"),
                name,
                source="unclassified", call_id=call,
            )
        # 5b. Authority that outlives the call -- see DURABLE_AUTHORITY_TOOLS.
        #     For a tool that grants authority the undo is "the operator
        #     revokes it later", which assumes the operator learned it
        #     happened, and an unanswered prompt is exactly the notice that
        #     did not reach them. Resolved after the explicit-allow branch at
        #     (3), so an operator who wants this unattended still has a
        #     written way to say so.
        if name in DURABLE_AUTHORITY_TOOLS:
            return Decision(
                DENY, active, risk,
                "%s grants authority that outlives this call, and there is no "
                "one here to be told it happened; run it from the console and "
                "answer the prompt, or write an explicit allow rule with "
                "/permissions" % (name or "(empty name)"),
                name,
                source="durable-authority", call_id=call,
            )
        # 5c. The effect classes fail closed. A prompt nobody answered is not
        #     a yes; the refusal names every route out so it is a gate, not a
        #     wall, and the receipt the caller leaves (see ``_observe``) is how
        #     an operator learns what unattended work wanted to happen.
        #
        #     One route out is answered right here: an operator may already
        #     have approved exactly this call (tool + argument digest). The
        #     approval is spent atomically on the way through, and only for a
        #     live decision -- a preflight neither spends one nor leaves a
        #     pending request behind.
        if risk in UNATTENDED_REFUSED_RISKS:
            ledger = _ledger_for(approval_ledger) if (digest and live) else None
            if ledger is not None:
                try:
                    approval = ledger.consume(name, digest, surface=surface)
                except Exception:
                    approval = None
                if approval is not None:
                    _SPENT_APPROVAL.set((name, digest))
                    return Decision(
                        ALLOW, active, risk,
                        "one-shot approval %s by %s covers exactly this call "
                        "(call %s) and is now spent" % (
                            getattr(approval, "nonce", "?"),
                            getattr(approval, "approver", "?"), call),
                        name, source="approval", call_id=call,
                    )
                try:
                    ledger.record_pending(
                        name, digest, surface=surface,
                        preview=argument_preview(arguments),
                    )
                except Exception:
                    # The ledger is a route out, not the gate: a ledger that
                    # cannot be written leaves the refusal exactly as it was.
                    pass
            _note_first_refusal()
            return Decision(
                DENY, active, risk, _unattended_reason(name, active, risk, call), name,
                source="unattended", call_id=call,
            )
        # 5d. The ``ask`` class proceeds on the record: it is the grade the
        #     catalog gives the chat, task and memory entry points, whose
        #     effects are gated tool by tool on the agent path. Refusing it
        #     would refuse every conversation held over MCP or HTTP.
        return Decision(
            ALLOW, active, risk,
            "no interactive prompt available; %s-class tools proceed "
            "unattended and are recorded" % risk,
            name,
            source="non-interactive", call_id=call,
        )
    reasons = {
        ALLOW: "%s allows %s tools" % (MODE_LABELS.get(active, active), risk),
        ASK: "%s asks before %s tools" % (MODE_LABELS.get(active, active), risk),
        DENY: "%s forbids %s tools" % (MODE_LABELS.get(active, active), risk),
    }
    return Decision(action, active, risk, reasons[action], name, source="mode",
                    call_id=call)


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
    order = ("safe", "ask", "mutation", "execution", "dangerous", UNCLASSIFIED)
    labels = {
        "safe": "read-only tools",
        "ask": "tools that touch the workspace",
        "mutation": "tools that change files",
        "execution": "tools that run host programs",
        "dangerous": "destructive/administrative tools",
        # Shown rather than hidden: the row reads "ask", but it is the one
        # row a non-interactive caller is refused on instead of allowed, and
        # ASK_CAVEAT below would otherwise state the opposite for it.
        UNCLASSIFIED: "tools nothing can classify",
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
        # One sentence, not a flat claim with a caveat filed underneath it: a
        # reader who stopped at the full stop after "auto." had read something
        # false, and nothing on that line invited them to read on.
        "  destructive tools ask in every mode, including auto, of a caller",
        "  who can be asked. %s" % ASK_CAVEAT,
    ]
    if elevated():
        lines.append("  PRIVILEGE: elevated%s" % (
            " - " + elevation_reason() if elevation_reason() else ""))
    return "\n".join(lines)
