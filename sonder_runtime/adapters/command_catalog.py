"""Canonical command catalog adapter -- one source of truth for every command.

Three surfaces used to drift apart: the REPL's ``elif cmd in (...)`` chain, the
``control_command`` chain the app and HTTP API call, and ``command_registry``'s
hand-written list.  The registry had 59 entries while the REPL answered 151
names and the MCP server registered 178 tools, so ``/commands`` confidently
under-reported the surface by more than half.

Nothing here is hand-maintained per command:

* Slash names and their alias groups are read out of the dispatch chains
  themselves (``ast`` over the ``cmd == "/x"`` / ``cmd in ("/x", "/y")``
  comparisons), so a new branch is catalogued the moment it is written.
* Every registered MCP tool is exposed as ``/<tool_name>`` with its real
  parameter schema, so all 178 are reachable without 178 new branches.
* Risk is read from the server's own policy sets rather than restated.

Only the category taxonomy and the "most used" ordering are curated, and the
category map falls back to prefix rules so a brand-new tool still lands
somewhere sensible instead of vanishing.

Stdlib only.  ``server`` is imported lazily inside functions: server imports
this module for /help, so a module-level import would be circular.  The
repository root is derived independently of this package location because
the catalog scans source files outside the adapter package.
"""
from __future__ import annotations

import ast
import difflib
import functools
import importlib
import os
import re
import shlex
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Mapping, Optional, Sequence

from sonder_runtime.application.ports.command_catalog import (
    CatalogCommand,
    CatalogInvocation,
    CommandCatalog,
)

if TYPE_CHECKING:
    from sonder_runtime.application.ports.command_catalog import CatalogCommand

_HERE = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)


class CommandCatalogProvider(CommandCatalog):
    """Compatibility object backed by this canonical module.

    The object preserves the pre-migration adapter surface used by ``server``
    while all implementation and private inspection attributes remain owned
    by this module.  Attribute lookup is deliberately dynamic so legacy
    imports and monkeypatches of the root identity redirect still work.
    """

    def __getattr__(self, name: str) -> Any:
        return globals()[name]

    def catalog(self) -> Sequence[CatalogCommand]:
        return globals()["catalog"]()

    def http_catalog(self) -> Sequence[CatalogCommand]:
        return globals()["http_catalog"]()

    def http_slash_tools(self) -> Mapping[str, Sequence[str]]:
        return globals()["http_slash_tools"]()

    def by_name(self, name: str) -> Optional[CatalogCommand]:
        return globals()["by_name"](name)

    def complete(self, prefix: str = "", **kwargs: Any) -> Sequence[CatalogCommand]:
        return globals()["complete"](prefix, **kwargs)

    def help_text(self, topic: str = "") -> str:
        return globals()["help_text"](topic)

    def parse_invocation(self, line: str) -> Optional[CatalogInvocation]:
        return globals()["parse_invocation"](line)


command_catalog = CommandCatalogProvider()

# The console and HTTP surfaces now live under ``sonder_runtime.interfaces``.
# Retain the root names as compatibility fallbacks for older checkouts while
# making source-derived catalog data follow the packaged implementations.
_SOURCE_PATHS = {
    "sonder_repl.py": os.path.join(
        "sonder_runtime", "interfaces", "repl", "repl.py",
    ),
    "sonder_serve.py": os.path.join(
        "sonder_runtime", "interfaces", "http", "serve.py",
    ),
}


def _source_path(name: str) -> str:
    """Resolve packaged source first, with a legacy-root fallback."""
    packaged = _SOURCE_PATHS.get(os.path.basename(name))
    if packaged:
        packaged_path = os.path.join(_HERE, packaged)
        if os.path.exists(packaged_path):
            return packaged_path
    return os.path.join(_HERE, name)


class CatalogUnavailable(RuntimeError):
    """The tool registry could not be read, so nothing here can be trusted.

    This used to be swallowed. Both ``console_tools()`` and ``catalog()``
    wrapped the registry read in ``except Exception:`` and carried on with an
    empty set, with a comment explaining that a partially initialised server
    must not break the gate -- but the code kept the gate from breaking by
    turning it off. Two things happened at once, and the quieter one was
    worse: every catalog-``dangerous`` tool downgraded to ``ask``, and
    ``console_tools()`` returned ``{}``, so the console's named-branch gate
    answered "allowed" for every command it maps.

    Both results are ``lru_cache(maxsize=1)``, so a *transient* failure --
    exactly the partially-initialised case the comments cited -- latched for
    the life of the process. Raising fixes that for free: an exception is not
    memoised, so the next call re-reads.

    Callers that enforce (``permission_modes.risk_of``, the console gate, the
    HTTP gate) treat this as "fail closed on ignorance" and refuse. Callers
    that merely display (``help_text``, ``format_matches``) say so in place of
    the listing rather than raising into a REPL loop.
    """


def _registered_tool_names(server) -> frozenset:
    """Every registered MCP tool name, or ``CatalogUnavailable``."""
    try:
        return frozenset(tool.name for tool in server.mcp._tool_manager.list_tools())
    except Exception as exc:
        raise CatalogUnavailable(
            "the MCP tool registry could not be read: %s" % exc
        ) from exc


# --- taxonomy -------------------------------------------------------------

CATEGORIES = {
    "basic": "Session, help, and getting around",
    "chat": "Ask the model, offload, consult, and route",
    "planning": "Tasks, plans, and checklists",
    "filesystem": "Find, read, write, and move guarded files",
    "repo": "Git history, status, diffs, and branches",
    "dev": "Test, lint, format, typecheck, build, and refactor",
    "execution": "Run code, scripts, programs, and projects",
    "agents": "Autonomous agents, fleets, autopilot, and workflows",
    "memory": "Lessons, facts, recall, and learning health",
    "context": "Context health, compaction, and sessions",
    "data": "Inspect, query, and convert structured data",
    "creative": "Artifacts, assets, images, and games",
    "web": "Search, fetch, weather, and location",
    "system": "Host, hardware, runtime policy, and diagnostics",
    "persona": "Tone, emotion vectors, and preferences",
    "security": "Permissions, risk inspection, and accounts",
    "training": "Curriculum, evaluation, and weight training",
}

# Explicit placements that the prefix rules would get wrong.
_CATEGORY_BY_TOOL = {
    "sonder": "chat",
    "offload": "chat",
    "extract_grounded": "chat",
    "consult": "chat",
    "ensemble_answer": "chat",
    "route_request": "chat",
    "agent": "agents",
    "workbench_agent": "agents",
    "loop": "agents",
    "mission_start": "agents",
    "mission_status": "agents",
    "composition_bindings": "agents",
    "improve_function": "dev",
    "codegen_build_loop": "dev",
    "scaffold_project": "dev",
    "project_detect": "dev",
    "repository_symbol_index": "dev",
    "dependency_inventory": "dev",
    "import_autofix": "dev",
    "json_patch": "filesystem",
    "text_patch": "filesystem",
    "text_search": "filesystem",
    "script_search": "filesystem",
    "program_search": "execution",
    "isolated_run": "execution",
    "sqlite_mutate": "data",
    "image_inspect": "creative",
    "ground_artifact": "creative",
    "weather_lookup": "web",
    "approximate_location_lookup": "web",
    "local_service_probe": "system",
    "diagnostics": "system",
    "status": "system",
    "unload": "system",
    "command_registry_list": "basic",
    "tool_manifest": "basic",
    "activity_status": "system",
    "debug_inspect": "system",
    "record_outcome": "memory",
    "apply_learned": "memory",
    "recall": "memory",
    "learning_health_status": "memory",
    "evaluation_history_status": "training",
    "promotion_eval": "training",
    "secret_scan": "security",
    "artifact_risk_inspect": "security",
    "process_memory_risk_inspect": "security",
    "process_list": "system",
    "set_context_size": "context",
    "session_export": "context",
    "sonder_sessions": "context",
    "sonder_stats": "memory",
    "sonder_remember_fact": "memory", "sonder_forget_fact": "memory",
    "hardware_profile": "system",
    "environment_status": "system",
    "toolchain_status": "system",
    "npu_status": "system",
    "cloud_opt_in": "security",
    "system_profile_text": "persona",
    "update_system_profile": "persona",
    "system_improvement_report": "system",
    # Source updates act on Sonder's repository checkout.  Keeping both the
    # read-only status and guarded fast-forward in the repo family makes
    # `/git` completion cohesive rather than surfacing unrelated system rows.
    "runtime_source_update": "repo",
    "runtime_source_update_status": "repo",
    "runtime_source_stash": "repo",
    "runtime_source_stash_status": "repo",
    "data_convert": "data",
    "elevate": "security",
}

# Ordered longest-first at use time; first hit wins.
_CATEGORY_BY_PREFIX = {
    "task_": "planning",
    "checklist_": "planning",
    "file_": "filesystem",
    "directory_": "filesystem",
    "archive_": "filesystem",
    "workspace_": "filesystem",
    "repo_": "repo",
    "git_": "repo",
    "test_": "dev",
    "lint_": "dev",
    "format_": "dev",
    "typecheck_": "dev",
    "build_": "dev",
    "dependency_": "dev",
    "rename_": "dev",
    "find_": "dev",
    "diff_": "dev",
    "apply_": "dev",
    "run_": "execution",
    "script_": "execution",
    "parallel_": "execution",
    "campaign_": "agents",
    "master_": "agents",
    "autopilot_": "agents",
    "workflow_": "agents",
    "memory_": "memory",
    "learn_": "memory",
    "learning_": "memory",
    "context_": "context",
    "data_": "data",
    "artifact_": "creative",
    "asset": "creative",
    "game_": "creative",
    "web_": "web",
    "admin_": "security",
    "permission_": "security",
    "runtime_": "system",
    "mcp_": "system",
    "live_": "system",
    "self_heal": "system",
    "system_": "system",
    "update_": "persona",
    "emotion_": "persona",
    "tune_emotion": "persona",
    "preferences_": "persona",
    "curriculum": "training",
    "eval": "training",
}

# The bare "/" menu. Ordered by how often they are actually reached for.
POPULAR = (
    "/help", "/todo", "/context", "/stats", "/work", "/read", "/search",
    "/files", "/run", "/agents", "/commands", "/activity",
)

# command_registry predates this taxonomy and uses two names it does not share.
_LEGACY_CATEGORY = {"inspect": "system", "learning": "memory"}

# Native console commands that front no MCP tool, so no schema names them.
_CATEGORY_BY_SLASH = {
    "/help": "basic", "/exit": "basic", "/new": "basic", "/clear": "basic", "/project": "basic",
    "/workspace": "filesystem", "/workspace-create": "filesystem", "/workspacecreate": "filesystem",
    "/sessions": "basic", "/replay": "basic", "/resume": "basic",
    "/version": "basic",
    "/model": "chat", "/persona": "persona", "/consult": "chat",
    "/route": "chat", "/refactor": "dev", "/scaffold": "dev",
    "/fact": "memory", "/facts": "memory", "/lessons": "memory",
    "/learn": "memory", "/good": "memory", "/bad": "memory",
    "/accept": "memory", "/pass": "memory", "/fail": "memory",
    "/todo": "planning", "/plan": "planning",
    "/cot": "system", "/debug": "system", "/env": "system", "/toolstatus": "system",
    "/trace": "system", "/strict": "system", "/dump": "system",
    "/whoami": "security", "/admin": "security", "/accounts": "security",
    "/login": "security", "/register": "security", "/setaccount": "security",
    "/approve": "security", "/approvals": "security",
    "/goal": "system", "/mission": "agents", "/improve": "system", "/append": "filesystem",
    "/updatecheck": "repo", "/update": "repo", "/updatesource": "repo",
    "/stash": "repo", "/runtime-stash": "repo",
    "/write": "filesystem", "/edit": "filesystem", "/delete": "filesystem",
    "/read": "filesystem", "/files": "filesystem", "/filepolicy": "filesystem",
    "/run": "execution", "/runwindow": "execution",
}

# A four-class ``_RISK_ORDER`` used to sit here -- no ``execution``, and, when
# it was removed, no readers anywhere in the tree. The live ranking is
# ``sonder_repl._RISK_ORDER``, which carries all five classes. A dead second
# copy of a policy vocabulary is worth less than nothing, because the next
# reader takes it for the vocabulary, so it is deleted rather than corrected.

# Names whose blast radius the policy sets alone understate.
_DANGEROUS = frozenset({
    "file_delete", "unload", "sqlite_mutate", "memory_privacy_repair",
    "memory_quality_repair", "admin_set_account", "admin_register",
    "git_merge", "git_cherry_pick", "task_delete", "self_heal_repair",
    "sonder_forget_fact",
    # Changing routing or the permission policy itself is not an ordinary
    # workspace edit: it alters what every later call is allowed to do, so it
    # must not flow unprompted in acceptEdits/auto. Elevation is the same
    # shape of decision -- it widens what every later privileged call is
    # allowed to do -- so it gets the same treatment.
    "runtime_policy_update", "permission_rule_set", "elevate", "cloud_opt_in",
    # A one-shot approval is authority another caller spends later; issuing
    # one is graded with the rule-writing tool it stands beside.
    "permission_approve",
    # Fast-forwarding the source tree changes the bytes enforcing every later
    # request.  The paired status tool is separately marked read-only below.
    "runtime_source_update",
    "runtime_source_stash",
    # `admin_login` is the console's elevation primitive by that same
    # definition, and was missing from this set because it looks like a read:
    # the login call mutates nothing itself. What it *returns* does. It is the
    # only way `sonder_repl.CURRENT_TOKEN` becomes non-empty, and that token is
    # threaded as `token=` into every guarded file op, where
    # `server._file_developer_allowed` turns it into `developer_authorized=` at
    # thirteen call sites. Measured: the identical `file_write` to a protected
    # control-plane path is refused before `/login` and succeeds after it, with
    # nothing else changed. That is `elevate` wearing a different name.
    "admin_login",
})

# Reads the server's own policy sets do not cover. Those two sets answer
# narrower questions -- what a *repository-scoped* agent may call
# (``REPOSITORY_READ_ONLY_TOOLS``) and what counts as having inspected the
# workspace before acting (``_WORK_INSPECTION_TOOLS``) -- so a tool can be
# plainly read-only and still belong in neither. ``task_list`` was exactly
# that: one ``SELECT`` and no writer anywhere beneath it, yet it landed in
# ``_risk_for``'s deliberately fail-closed ``ask`` default, which ``plan``
# denies. Plan exists to stop Sonder *changing* things, not to stop it
# answering a question, so a verified read is named here rather than pushed
# into a server set whose other meanings it does not carry -- adding
# ``task_list`` to ``_WORK_INSPECTION_TOOLS`` would have made listing todos
# count as having inspected the workspace.
#
# Membership is a claim that the tool has no write path, checked by reading
# it. It is not a place to park a tool that merely looks harmless: the
# ``ask`` default above is the right answer for anything unverified.
_READ_ONLY = frozenset({
    "task_list", "task_show", "checklist_show", "admin_status",
    "admin_whoami", "autopilot_status", "calibration_status", "learn_tiers",
    "live_reload_status", "mcp_runtime_status", "reasoning_show",
    "sonder_sessions", "sonder_stats", "turn_inspect", "workflow_list",
    "memory_export", "policy_explain",
    "runtime_source_update_status",
    "runtime_source_stash_status",
    "permission_approvals",
})

# Branches whose real work is done by module-level functions that front no
# registered MCP tool, mapped to the name the gate should grade them by.
#
# ``_branch_tool_calls`` keeps only names the registry knows, so ``_open_db``
# and friends drop out. That filter is fail-OPEN at the *command* level: a
# branch whose writer is a plain module function is classified by whatever
# registered tool it incidentally also calls. ``/selfmod`` was the live case
# -- ``selfmod.deploy`` ``os.replace``s Sonder's own source tree
# (``selfmod.py``), and the only registered tool in that branch is
# ``system_improvement_report``, a ``safe`` read it quotes as evidence. So the
# gate was consulted, took the strictest member of a one-element ``safe`` set,
# and answered "allowed" under ``plan``. A confident wrong answer, not a gap.
#
# The remedy is to make the command stand for its own work: the name here is
# added to the branch's tool list, and ``permission_modes.risk_of`` resolves it
# through this catalog's entry for ``/selfmod``, whose curated risk
# (``command_registry``) now says what the command actually does. It is a
# floor, not a replacement -- the strictest of it and every tool the branch
# calls still decides.
#
# Deliberately a short, written-down list rather than a general rule, and that
# is a measurement rather than a preference. Two general rules were tried over
# both console chains against the 185 registered tools:
#
#   "any call this walk could not resolve fails closed" flags 94 of the 124
#   mapped commands -- ``json.dumps``, ``arg.strip()`` and every
#   ``server.control_command`` delegation among them;
#
#   "grade a command by its own catalogued risk" flags 19, of which 18 are
#   verified reads (``/read``, ``/search``, ``/tree``, ``/files``).
#
# Both would put ordinary reads behind a prompt and deny them under ``plan``,
# which is the over-refusal this gate is explicitly built to avoid -- a
# refusal nobody can act on trains operators to route around the gate. A rule
# that fires on 94 of 124 commands is not a gate; it is noise with a verdict.
# ``/mcp refresh`` republishes the live MCP source and tool registry, which
# changes what every later call is allowed to do -- the same reasoning
# ``_DANGEROUS`` already applies to ``runtime_policy_update`` and
# ``permission_rule_set``. ``/goal`` writes to ``goal_store`` (adopt, complete,
# abandon, note), an ordinary write, so it is graded ``mutation`` and flows in
# ``acceptEdits`` where the registry refresh still does not.
#
# ``/report`` was read the same way and deliberately left out: it only formats
# ``activity_tracker.latest()``, so there is nothing there to gate and adding
# it would be over-refusal dressed as caution.
# ``/runwindow`` and its aliases launch a code block in a detached console via
# ``code_runner.run_code_window``, which fronts no tool -- so ``/run`` was
# refused under ``plan`` while ``/runwindow`` ran the same block. Graded
# ``execution`` through ``permission_modes.EXECUTION_COMMANDS``.
#
# ``/training`` passes its argument to ``adaptive_training.command_text``,
# which runs that CLI's own ``main()``: ``start``, ``deploy``, ``rollback``,
# ``adopt-legacy``, ``release-alias``. Deploying an adapter changes which
# weights every later call uses, so it is graded ``dangerous`` for the reason
# ``runtime_policy_update`` is.
#
# ``/hardware`` reaches the same CLI but only ever with a *literal* argument,
# so it cannot reach those subcommands. It is recorded here as the read it is
# rather than put on the coverage floor's display-only allow-list: a recorded
# ``safe`` verdict and an exemption behave identically today and differ
# entirely tomorrow -- the verdict is visible to ``/permissions``, can be
# overridden by a rule, and stays inside the map this repo now has a floor
# for. The literal is asserted by a test, because it is the only thing that
# makes ``safe`` true.
_UNREGISTERED_BRANCH_WORK = {
    # The scoped console facade gates the immutable prepared command itself.
    # Declare its work here even though static discovery cannot follow it.
    "/lanes": "agent_lane",
    "/recover": "workspace_run",
    "/selfmod": "selfmod",
    "/selfmodify": "selfmod",
    "/mcp": "mcp",
    "/convergence": "mcp",
    "/goal": "goal",
    "/goals": "goal",
    # The persistent autonomy command is native (its lifecycle calls are not
    # individual catalogued MCP tools), so static tool-call discovery sees no
    # work below either slash branch.  Keep both aliases on the permission
    # floor through their registered native command rather than letting an
    # empty tool set read as allowed.
    "/autopilot": "autopilot",
    "/auto": "autopilot",
    "/mission": "mission",
    "/runwindow": "runwindow",
    "/runnew": "runwindow",
    "/runconsole": "runwindow",
    "/training": "training",
    "/weighttraining": "training",
    "/hardware": "hardware",
    # `/location on` grants session-wide approximate IP-geolocation consent by
    # assigning the REPL's `location_consent`, which every later turn is then
    # handed. No registered tool fronts that assignment -- `/location` never
    # calls `approximate_location_lookup`, it authorizes it -- so without an
    # entry here the console gate resolves the command to an empty tool set
    # and allows it unconditionally. It sat on the coverage floor's
    # display-only allow-list instead, under a reason ("reads the
    # location-consent env flag") that described only the bare form. Recorded
    # as the grant it is, for the reason `/hardware` is recorded as the read
    # it is: a verdict is visible to `/permissions`, can be overridden by a
    # rule, and stays inside the map the floor checks.
    "/location": "location",
}


@dataclass(frozen=True)
class Param:
    name: str
    type: str
    required: bool
    default: object = None

    def render(self) -> str:
        if self.required:
            return "<%s>" % self.name
        # Compare against None/"" explicitly. `0 in (None, "", False)` is True
        # because 0 == False, so a membership test silently hid every int
        # default of 0 from the usage line while help_command's table still
        # printed it -- the two disagreed on 62 parameters.
        if self.default is None or self.default == "":
            return "[%s]" % self.name
        return "[%s=%s]" % (self.name, self.default)


@dataclass(frozen=True)
class Command:
    name: str
    aliases: tuple
    tool: str
    category: str
    risk: str
    summary: str
    params: tuple
    native: bool

    @property
    def all_names(self) -> tuple:
        return (self.name,) + tuple(self.aliases)

    def usage(self) -> str:
        if not self.params:
            return self.name
        return "%s %s" % (self.name, " ".join(p.render() for p in self.params))

    def to_dict(self) -> dict:
        return {
            "name": self.name, "aliases": list(self.aliases), "tool": self.tool,
            "category": self.category, "risk": self.risk,
            "summary": self.summary, "native": self.native,
            "usage": self.usage(),
            "params": [
                {"name": p.name, "type": p.type, "required": p.required,
                 "default": p.default}
                for p in self.params
            ],
        }


# Native console branches predate the MCP schema registry, but several still
# have a real positional contract.  Publishing that contract keeps /help,
# completion, the HTTP catalog, and bounded probes from advertising a bare
# command that can only answer with a usage error.  These are syntax-only
# descriptions; the branch remains the authority for validation and policy.
_NATIVE_PARAM_SPECS = {
    "/artifactcheck": (
        Param("path", "str", True), Param("recipe", "str", False, "auto"),
    ),
    "/asset": (
        Param("name", "str", True), Param("brief", "str", True),
    ),
    "/weather": (Param("location", "str", True),),
    "/ensemble": (Param("question", "str", True),),
    "/work": (Param("prompt", "str", True),),
    "/agentcancel": (Param("agent_id", "str", True),),
    "/agentretry": (
        Param("agent_id", "str", True), Param("tier", "str", False, ""),
    ),
    "/capacity": (Param("requested_agents", "int", False, 0),),
    # The branch parses `language dimension name | concept` as one command
    # line, so a single named field documents the actual free-form grammar.
    "/game": (Param("spec", "str", True),),
    "/gamefleet": (Param("spec", "str", True),),
    # `[id|title] [N]` -- both optional; a bare /replay shows this thread.
    "/replay": (
        Param("thread", "str", False, ""), Param("turns", "int", False, 20),
    ),
}


def _native_params(name: str) -> tuple:
    return _NATIVE_PARAM_SPECS.get(name, ())


# --- derivation -----------------------------------------------------------


def _slash_groups(path: str) -> list[tuple[str, ...]]:
    """Alias groups read out of a dispatch chain's ``cmd`` comparisons.

    ``elif cmd in ("/cot", "/thoughts")`` yields ``("/cot", "/thoughts")`` so
    the catalog can show one command with two spellings rather than two
    commands that happen to do the same thing.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
    except (OSError, SyntaxError):
        return []
    groups: list[tuple[str, ...]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare) or len(node.ops) != 1:
            continue
        if not isinstance(node.left, ast.Name) or node.left.id != "cmd":
            continue
        comparator = node.comparators[0]
        names: list[str] = []
        if isinstance(node.ops[0], ast.Eq) and isinstance(comparator, ast.Constant):
            if isinstance(comparator.value, str):
                names = [comparator.value]
        elif isinstance(node.ops[0], ast.In) and isinstance(
            comparator, (ast.Set, ast.Tuple, ast.List)
        ):
            names = [
                item.value for item in comparator.elts
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            ]
        # len > 1 drops the bare "/" branch: that is the "show me what I can
        # type" gesture, not a command, and cataloguing it puts an entry named
        # "/" in every help listing.
        names = [n for n in names if n.startswith("/") and len(n) > 1]
        if names:
            groups.append(tuple(dict.fromkeys(names)))
    return groups


def _native_groups() -> list[tuple[str, ...]]:
    """Alias groups, taking ``control_command`` as authoritative.

    The two chains do not mean the same thing by ``cmd in (...)``.  In
    ``server.control_command`` each branch is one command and the tuple is its
    real alias list.  In the REPL several branches are *delegation* lists --
    ``elif cmd in ("/report", "/endreport", "/checklist", "/plan", ...)``
    simply forwards all of them to ``control_command`` -- so reading those as
    aliases collapsed 22 unrelated commands into one entry and gave ``/grep``
    the summary belonging to ``/report``.

    So: take server.py's grouping first, then let the REPL contribute only the
    names server.py never defines (``/todo``, ``/exit``, ``/model``, ...).
    """
    groups: list[tuple[str, ...]] = []
    claimed: set[str] = set()
    for path in ("server.py", "sonder_repl.py"):
        for group in _slash_groups(_source_path(path)):
            fresh = tuple(n for n in group if n not in claimed)
            if fresh:
                groups.append(fresh)
                claimed.update(fresh)
    return groups


@functools.lru_cache(maxsize=1)
def http_native_names() -> frozenset:
    """Slash spellings the HTTP chat dispatcher handles directly.

    ``catalog()`` intentionally describes the union of the console and MCP
    surfaces.  The Flutter client, however, sends its selected slash command
    to ``sonder_serve._handle_slash``.  Reading that one dispatcher here keeps
    the HTTP index from advertising console-only controls such as ``/model``
    and ``/project`` that would otherwise fall through to the model as prose.
    """
    path = _source_path("sonder_serve.py")
    try:
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
    except (OSError, SyntaxError):
        return frozenset()
    dispatch = next(
        (
            node for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "_handle_slash"
        ),
        None,
    )
    if dispatch is None:
        return frozenset()
    names: set[str] = set()
    for node in ast.walk(dispatch):
        if isinstance(node, ast.Compare):
            names.update(_branch_names(node))
    return frozenset(names)


# ``control_command`` is a dispatch chain, not a worker. It is resolved by
# name (see ``http_slash_tools``), and following it as a function would grade
# every branch that forwards to it by everything the whole chain can do.
_NEVER_FOLLOW = frozenset({"control_command"})


@functools.lru_cache(maxsize=None)
def _module_level_functions(module: str) -> dict:
    """Top-level ``def``s of a sibling module in this repo, or ``{}``.

    Module level only: a nested helper is not reachable as
    ``<module>.<name>``, so admitting one would attribute a call that cannot
    happen.
    """
    path = _source_path(module + ".py")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
    except (OSError, SyntaxError):
        return {}
    return {
        node.name: node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _wrapper_tools(module: str, name: str, tool_names: frozenset, seen: set) -> set:
    """Registered tools called directly inside ``<module>.<name>``.

    Deliberately shallow -- the tools that one function calls itself, and no
    further. Depth is where this turns into over-attribution: two levels out
    of a general-purpose helper starts crediting a branch with work it cannot
    reach, and over-refusal trains operators to route around the gate.
    """
    key = "%s.%s" % (module, name)
    if key in seen:
        return set()
    seen.add(key)
    target = _module_level_functions(module).get(name)
    if target is None:
        return set()
    found = set()
    for child in ast.walk(target):
        if not isinstance(child, ast.Call):
            continue
        inner = child.func
        if isinstance(inner, ast.Attribute) and inner.attr in tool_names:
            found.add(inner.attr)
        elif isinstance(inner, ast.Name) and inner.id in tool_names:
            found.add(inner.id)
    return found


def _branch_tool_calls(path: str, function: str, tool_names: frozenset) -> dict:
    """``{"/write": ("file_write",)}`` for one hand-written dispatch chain.

    Read out of the source rather than hand-listed, for the same reason the
    alias groups above are: a console branch that calls a real tool has to be
    covered by the permission gate the moment it is written, not the next time
    someone remembers to update a table. Hand-maintained security tables go
    stale silently, and a permission gate that silently stops covering a
    command is worse than none, because the mode indicator keeps claiming it.

    ``/write`` runs ``file_write`` but is not *named* ``file_write``, so the
    catalog's own name-matching (``Command.tool``) cannot see it: that only
    fronts a tool when a slash name and a tool name coincide. This walks the
    branch body instead, following module-level helpers one level deep
    (``do_refactor`` -> ``ensemble_answer``) and keeping only names that are
    actually registered MCP tools, so ``_open_db`` and friends drop out.
    """
    try:
        with open(_source_path(path), encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
    except (OSError, SyntaxError):
        return {}
    functions = {
        node.name: node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    scope = functions.get(function)
    if scope is None:
        return {}

    def _called_tools(node, depth: int, seen: set) -> set:
        found: set = set()
        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue
            target = child.func
            if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
                # Any ``<module>.<tool_name>``, not just ``server.<tool_name>``.
                # ``/run`` executes host code through ``code_runner.run_code``
                # and never touches the ``run_code`` tool, so a receiver check
                # would have left the console's most obviously "runs a program"
                # command outside a gate whose whole job is running programs.
                # The name is filtered against the real registry below.
                found.add(target.attr)
                # ...and when the name is NOT a tool, it may be a wrapper in
                # another module of this repo that calls the tools for it.
                # ``sonder_serve``'s ``/emotion`` branch calls
                # ``server.emotion_command``, so it resolved to nothing while
                # the console's own branch -- which calls the tools directly --
                # resolved to three: the same command, the same write, refused
                # for the operator and allowed for the app. One level only, and
                # never into ``control_command``, which is a 97-branch dispatch
                # chain: following that grades every delegating branch by the
                # union of all of them (measured: 49 branches jump to
                # ``dangerous``). It is resolved by name instead.
                if (
                    target.attr not in tool_names
                    and target.attr not in _NEVER_FOLLOW
                    and depth < 1
                ):
                    found |= _wrapper_tools(
                        target.value.id, target.attr, tool_names, seen,
                    )
            elif isinstance(target, ast.Name):
                if target.id in tool_names:
                    found.add(target.id)
                elif (
                    target.id in functions
                    and target.id not in seen
                    and depth < 2
                ):
                    seen.add(target.id)
                    found |= _called_tools(functions[target.id], depth + 1, seen)
        return found

    found: dict = {}
    for node in ast.walk(scope):
        if not isinstance(node, ast.If):
            continue
        names = _branch_names(node.test)
        if not names:
            continue
        body = ast.Module(body=node.body, type_ignores=[])
        tools = sorted(_called_tools(body, 0, set()) & tool_names)
        if not tools:
            continue
        for name in names:
            found[name] = tuple(sorted(set(found.get(name, ())) | set(tools)))
    return found


def _branch_names(test) -> list[str]:
    """Slash names compared in one ``cmd == "/x"`` / ``cmd in ("/x", "/y")`` test."""
    if not isinstance(test, ast.Compare) or len(test.ops) != 1:
        return []
    if not isinstance(test.left, ast.Name) or test.left.id != "cmd":
        return []
    comparator = test.comparators[0]
    values: list = []
    if isinstance(comparator, ast.Constant):
        values = [comparator.value]
    elif isinstance(comparator, (ast.Tuple, ast.List, ast.Set)):
        values = [
            item.value for item in comparator.elts
            if isinstance(item, ast.Constant)
        ]
    return [
        value.lower() for value in values
        if isinstance(value, str) and value.startswith("/") and len(value) > 1
    ]


# Keyword arguments that remove the capability a tool is graded for, when the
# CALL SITE pins them to a literal the caller cannot influence.
#
# This exists for the inverse of the tool-less default, and it is deliberately
# tiny. `/delete` is graded `dangerous` -- correctly, for the tool -- but the
# console branch is `server.file_delete(path=..., dry_run=True, token=...)`
# with the literal written in. There is no console spelling that reaches a real
# delete, so the operator is warned about a deletion that cannot happen. That
# is the same class of wrong answer as grading `/setaccount` safe, and it is
# why "grade tool-less commands stricter" is not the fix for #47: it would make
# this error worse rather than better.
#
# Two properties keep this from becoming a bypass:
#   * It is evidence-based. The literal must be visible in the AST at the call
#     site. A variable, an expression, or a caller-supplied value does not
#     match, so nothing that could be `False` at runtime is ever de-escalated.
#   * It attaches to a CALL SITE, never to a tool. `/file_delete` -- the direct
#     MCP spelling, which takes `dry_run` from whoever calls it -- is untouched
#     and stays `dangerous`.
_DISARMING_ARGUMENTS = {
    "file_delete": {"dry_run": True},
}


def _disarmed_branch_tools(path: str, function: str) -> dict:
    """``{slash: frozenset(tools this branch can only call disarmed)}``.

    A tool counts as disarmed for a branch only if the branch calls it at least
    once and *every* call in that branch pins every disarming argument to the
    required literal. One armed call anywhere in the branch re-arms it, so
    adding a second `file_delete(...)` call without `dry_run=True` restores the
    `dangerous` grade instead of inheriting the exemption.
    """
    try:
        with open(_source_path(path), encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
    except (OSError, SyntaxError):
        return {}
    functions = {
        node.name: node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    scope = functions.get(function)
    if scope is None:
        return {}

    def _classify(node) -> tuple:
        """(tools called, tools called with every disarming literal pinned)."""
        called: set = set()
        disarmed: set = set()
        armed: set = set()
        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue
            target = child.func
            if isinstance(target, ast.Attribute):
                name = target.attr
            elif isinstance(target, ast.Name):
                name = target.id
            else:
                continue
            required = _DISARMING_ARGUMENTS.get(name)
            if required is None:
                continue
            called.add(name)
            supplied = {
                kw.arg: kw.value for kw in child.keywords
                if kw.arg is not None
            }
            ok = True
            for key, needed in required.items():
                value = supplied.get(key)
                if not isinstance(value, ast.Constant) or value.value is not needed:
                    ok = False
                    break
            (disarmed if ok else armed).add(name)
        return called, disarmed - armed

    found: dict = {}
    for node in ast.walk(scope):
        if not isinstance(node, ast.If):
            continue
        names = _branch_names(node.test)
        if not names:
            continue
        body = ast.Module(body=node.body, type_ignores=[])
        _, disarmed = _classify(body)
        if not disarmed:
            continue
        for name in names:
            found[name] = frozenset(found.get(name, frozenset()) | disarmed)
    return found


@functools.lru_cache(maxsize=1)
def console_disarmed_tools() -> dict:
    """`_disarmed_branch_tools` across both console dispatch chains."""
    merged: dict = {}
    for path, function in (
        ("server.py", "control_command"), ("sonder_repl.py", "main"),
    ):
        for slash, tools in _disarmed_branch_tools(path, function).items():
            merged[slash] = frozenset(merged.get(slash, frozenset()) | tools)
    return merged


@functools.lru_cache(maxsize=1)
def console_tools() -> dict:
    """Every named console command mapped to the MCP tools it can invoke.

    Covers both hand-written chains a typed slash command can reach: the
    REPL's own ``main`` and ``server.control_command``, which the REPL
    delegates ~25 names to. ``/mkdir`` therefore resolves to
    ``directory_create`` even though only ``control_command`` names it.

    This is what lets the console permission gate sit at ONE choke point --
    the top of the REPL's slash chain -- instead of at each of the ~50
    branches. Commands that front no tool (``/help``, ``/trace``, ``/exit``)
    are simply absent, and are not gated.
    """
    server = importlib.import_module("server")  # lazy compatibility edge

    names = _registered_tool_names(server)
    merged: dict = {}
    for path, function in (
        ("server.py", "control_command"), ("sonder_repl.py", "main"),
    ):
        for slash, tools in _branch_tool_calls(path, function, names).items():
            merged[slash] = tuple(sorted(set(merged.get(slash, ())) | set(tools)))
    return _with_unregistered_work(merged)


def _with_unregistered_work(mapped: dict) -> dict:
    """Add the stand-in name for branches whose real work fronts no tool.

    Unconditional, and that is the point. An earlier version only augmented
    keys the derivation had already produced -- which made ``/selfmod``'s
    floor depend on the branch still calling ``system_improvement_report``,
    the incidental ``safe`` sibling that caused the defect in the first place.
    Drop that one call and the key disappears, taking the floor with it and
    silently restoring the exact hole it was added to close. A floor
    conditional on the thing it is protecting against is not a floor.

    ``/mcp`` and ``/goal`` are here for the same reason and were never in the
    derivation at all: neither branch calls a registered tool, so both mapped
    to nothing and were ungated.
    """
    out = dict(mapped)
    for slash, stand_in in _UNREGISTERED_BRANCH_WORK.items():
        out[slash] = tuple(sorted(set(out.get(slash, ())) | {stand_in}))
    return out


# --- read-branch narrowing --------------------------------------------------
#
# The chain gates grade a slash command by the strictest tool it can front,
# because they run before the branch's own argument grammar has chosen a
# member. That is the right default for an argument nothing here recognises
# (one prompt, worst case named) and the wrong one for a read once the effect
# classes fail closed for unattended callers: ``/selfmod status`` would be
# refused for the ``deploy`` its sibling branch can reach, and ``/todo list``
# for ``task_delete``. These rules name the member a *recognisable read form*
# actually reaches, the bare form included wherever the branch treats it as
# one (``/todo`` alone lists, ``/selfmod`` alone shows status). They are
# deliberately literal to each branch's grammar -- an argument this table does
# not recognise keeps the whole union -- and each stand-in they produce for
# work that fronts no registered tool is graded in
# ``permission_modes.READ_BRANCH_WORK`` and pinned by a drift test.

READ_STAND_INS = frozenset({"selfmod_status", "goal_status", "training_status"})

_STATUS_FORMS = ("", "status", "list", "show")
# ``server._selfmod_command`` treats the bare form as ``status`` and these
# actions as reads of recorded runs (``tests`` reports stored results and runs
# nothing). ``plan``, ``run``, ``deploy``, ``rollback``, ``mode``,
# ``retention`` and ``prune-backups`` keep the strictest grade.
_SELFMOD_READ_ACTIONS = frozenset({
    "", "status", "show", "list", "history", "inspect", "diff", "tests", "backups",
})
# ``server._mcp_command`` treats the bare form as ``status``, reads for these
# and reloads for ``refresh``; the registered ``mcp_runtime_status`` tool
# renders the same report.
_MCP_READ_ACTIONS = frozenset({"", "status", "show", "audit", "list", "help", "?"})
# ``server._goal_command`` treats the bare form as ``show``; ``set``/``note``/
# ``done``/``abandon``/``refresh``/``adopt``/``decline`` mutate, and these
# only read.
_GOAL_READ_ACTIONS = frozenset({"", "show", "status", "history", "proposals"})
# ``server._training_command`` renders the plan for the bare form and a plan
# or a status for these; anything else (``start``, ``deploy``, ``rollback``)
# reaches the attended lifecycle.
_TRAINING_READ_ACTIONS = frozenset({"", "plan", "status", "hardware", "help", "?"})
# The bare form lists in both branches that carry this command
# (``sonder_repl.main`` and ``sonder_serve._handle_slash``); an action neither
# grammar names keeps the union, which a command that can delete grades at
# its delete.
_TASK_ACTIONS = {
    "": ("task_list",), "list": ("task_list",), "ls": ("task_list",),
    "show": ("task_show",), "view": ("task_show",),
    "progress": ("task_progress",), "status": ("task_progress",),
    "add": ("task_create",), "create": ("task_create",), "new": ("task_create",),
    "done": ("task_update",), "complete": ("task_update",), "finish": ("task_update",),
    "start": ("task_update",), "doing": ("task_update",),
    "block": ("task_update",), "blocked": ("task_update",),
    "plan": ("task_plan",),
    "delete": ("task_delete",), "rm": ("task_delete",), "remove": ("task_delete",),
    "depend": ("task_depend",), "dep": ("task_depend",), "blockedby": ("task_depend",),
}


def narrow_branch_tools(cmd, argument, tools):
    """The tools the recognised read form of ``cmd`` reaches, else ``tools``.

    ``tools`` is the chain's union for ``cmd`` and is returned unchanged for
    every argument this table does not recognise, so narrowing can only ever
    remove members the argument grammar proves unreachable. The bare form is
    recognised exactly where the branch treats it as a read (``/todo`` lists,
    ``/selfmod`` and ``/mcp`` show status, ``/goal`` shows, ``/training``
    plans); a branch whose bare form does anything else keeps its union. Members of a
    narrowed set that the union does not contain are kept only when they are
    the declared read stand-ins or the read-only status tools the surfaces
    already substitute (``emotion_vector_status`` for ``/emotion status``);
    a task or fact member absent from the union means the surface does not
    support that operation, and the union is kept so the surface refuses it
    as it always did.
    """
    command = str(cmd or "").strip().lower()
    words = str(argument or "").strip().split(None, 1)
    action = words[0].lower() if words else ""
    union = tuple(tools or ())
    if command in ("/emotion", "/emotions", "/vectors", "/mood") and action in _STATUS_FORMS:
        return ("emotion_vector_status",)
    if command in ("/prefer", "/preference", "/preferences") and action in _STATUS_FORMS:
        return ("preferences_status",)
    if command in ("/contextsize", "/ctxsize") and action == "":
        return ("context_policy_status",)
    if command in ("/runtime", "/models") and action in ("", "status"):
        return ("runtime_policy_status",)
    if command in ("/stash", "/runtime-stash") and action in ("", "status", "list"):
        return ("runtime_source_stash_status",)
    if command in ("/selfmod", "/selfmodify"):
        if action == "opportunities":
            return ("system_improvement_report",)
        if action in _SELFMOD_READ_ACTIONS:
            return ("selfmod_status",)
        return union
    if command in ("/mcp", "/convergence") and action in _MCP_READ_ACTIONS:
        return ("mcp_runtime_status",)
    if command in ("/goal", "/goals") and action in _GOAL_READ_ACTIONS:
        return ("goal_status",)
    if command in ("/training", "/weighttraining") and action in _TRAINING_READ_ACTIONS:
        return ("training_status",)
    if command in ("/task", "/tasks", "/todo") and action in _TASK_ACTIONS:
        narrowed = _TASK_ACTIONS[action]
        return narrowed if all(name in union for name in narrowed) else union
    if command == "/fact" and action:
        narrowed = ("sonder_forget_fact",) if action == "forget" else ("sonder_remember_fact",)
        return narrowed if all(name in union for name in narrowed) else union
    return union


# Marker for a branch that is a one-line forward to ``server.control_command``.
# It is not a tool; it is resolved away below into whatever that chain's own
# branch of the same name reaches.
_DELEGATION = "control_command"

# The served task aliases deliberately call the non-MCP scoped dispatcher so
# an authenticated account cannot choose its storage scope.  Static call
# discovery cannot follow that dispatcher without either treating its whole
# dynamic dispatch as every task operation or missing the permission-gate
# contract entirely.  Keep this narrow, explicit declaration in the catalog
# that owns the HTTP gate; the HTTP surface supports only these four task
# operations for the aliases (the richer task API remains catalogued by name).
_HTTP_SCOPED_TASK_ALIAS_TOOLS = (
    "task_create", "task_list", "task_show", "task_update",
)


@functools.lru_cache(maxsize=1)
def http_slash_tools() -> dict:
    """Every app/HTTP slash command mapped to the MCP tools it can invoke.

    ``sonder_serve._handle_slash`` is a sixth hand-written dispatch chain and
    the one the Flutter app talks to. It is derived here for exactly the reason
    the console's map is: it calls ``server.file_write``/``file_edit``/
    ``file_delete`` directly and forwards ten more names to
    ``server.control_command``, so a hand-kept table would go stale the first
    time a branch was added, and a permission gate that silently stops covering
    a command is worse than none.

    Kept separate from ``console_tools()`` rather than merged into it: the two
    chains answer the same slash names with *different* bodies. Measured over
    the 110 names both chains define, three differ -- ``/todo`` and its aliases
    reach ``task_delete``/``task_plan``/``task_depend``/``task_progress`` at
    the console and only ``task_create``/``list``/``show``/``update`` here --
    so unioning them would gate each surface at the other's blast radius, and
    an operator refused a delete here could not tell why.

    The forwards are resolved rather than dropped. A branch whose whole body is
    ``return server.control_command(...)`` names no tool of its own, so
    attributing it nothing would have left those ten as ungated as the chain
    was -- they inherit ``control_command``'s own branch of the same name.
    """
    server = importlib.import_module("server")  # lazy compatibility edge

    names = _registered_tool_names(server)
    delegated = _branch_tool_calls("server.py", "control_command", names)
    own = _branch_tool_calls(
        "sonder_serve.py", "_handle_slash", names | {_DELEGATION},
    )
    merged: dict = {}
    for slash, tools in own.items():
        resolved = set(tools)
        if _DELEGATION in resolved:
            resolved.discard(_DELEGATION)
            resolved |= set(delegated.get(slash, ()))
        if resolved:
            merged[slash] = tuple(sorted(resolved))
    for slash in ("/todo", "/task", "/tasks"):
        merged[slash] = _HTTP_SCOPED_TASK_ALIAS_TOOLS
    return _with_unregistered_work(merged)


@functools.lru_cache(maxsize=1)
def _help_summaries() -> dict:
    """One-liners harvested from the REPL's hand-written ``HELP`` block.

    Native console commands (``/trace``, ``/model``, ``/persona``) front no MCP
    tool, so no schema describes them, and most predate ``command_registry``.
    Their descriptions already exist in that block -- read them rather than
    restate them.  Parsed from source, not imported: ``sonder_repl`` imports
    ``server``, which imports this module.
    """
    try:
        with open(_source_path("sonder_repl.py"), encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
    except (OSError, SyntaxError):
        return {}
    block = ""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "HELP" in targets and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                block = node.value.value
                break
    out: dict[str, str] = {}
    for line in block.splitlines():
        match = re.match(r"^\s+(/[a-z]\w*)", line)
        if not match:
            continue
        # The block is column-aligned, but a long argument list can eat the
        # gutter ("/model [name|tier] list installed..."), so fall back to the
        # description column when the two-space split finds only one chunk.
        chunks = [c for c in re.split(r"\s{2,}", line.strip()) if c]
        description = chunks[-1] if len(chunks) > 1 else line[21:].strip()
        if description and not description.startswith("/"):
            out.setdefault(match.group(1), description)
    return out


def _category_for(name: str) -> str:
    if name in _CATEGORY_BY_TOOL:
        return _CATEGORY_BY_TOOL[name]
    for prefix in sorted(_CATEGORY_BY_PREFIX, key=len, reverse=True):
        if name.startswith(prefix):
            return _CATEGORY_BY_PREFIX[prefix]
    return "system"


def _is_execution(name: str) -> bool:
    """Whether the gate classes ``name`` as starting a host process.

    Reads ``permission_modes``' two frozensets directly rather than calling
    ``permission_modes.risk_of``. ``risk_of`` resolves the catalogued risk via
    ``by_name``, which builds this very catalog -- calling it from inside
    ``catalog()`` would recurse through a half-populated ``lru_cache``. The
    sets carry no catalog dependency, and the precedence that matters
    (``dangerous`` before ``execution``, so ``self_heal_repair`` cannot report
    as merely executable) is preserved by the caller's ordering below, exactly
    as ``risk_of`` orders it.
    """
    permission_modes = importlib.import_module(
        "permission_modes"
    )  # lazy: permission_modes imports this module

    stem = str(name or "").strip().lstrip("/")
    if not stem:
        return False
    return (
        stem in permission_modes.EXECUTION_TOOLS
        or stem in permission_modes.EXECUTION_COMMANDS
    )


def _declared_native_risk(stem: str, hit) -> str:
    """Risk for a native command that fronts no registered tool.

    ``EXECUTION_COMMANDS`` exists precisely for this shape -- branch work no
    tool name stands for -- so it is consulted before the legacy registry's
    curated value, which predates the class and cannot name it.
    """
    if _is_execution(stem):
        return "execution"
    return hit.get("risk", "safe") if hit else "safe"


def _risk_for(name: str, server) -> str:
    """The risk class published for a tool -- the one the gate decides on.

    ``execution`` sits here rather than only in ``permission_modes.risk_of``
    because this value is what ``GET /v1/commands`` ships to the Flutter app,
    and the app pairs it with the ``matrix`` from ``GET /v1/permission-mode``,
    whose rows are keyed by the gate's five classes. While this function
    emitted only four, 27 of 273 commands reached the app under a class the
    gate does not decide on -- ``/build_run``, ``/test_run``, ``/typecheck_run``
    and ``/dependency_audit`` among them as ``safe``, drawn with the green dot
    and the words "Safe - read only", for tools ``plan`` refuses outright.
    """
    if name in _DANGEROUS:
        return "dangerous"
    if _is_execution(name):
        return "execution"
    if name in getattr(server, "_WORK_MUTATION_TOOLS", frozenset()):
        return "mutation"
    # A tool that resolves a caller-supplied root with no allowed-roots check is
    # never "safe", however read-only its own body is: `plan` allows everything
    # classed safe, and reading an arbitrary directory on the machine is not
    # what "reads only - no writes, no commands" promises. Checked before the
    # read-only sets because all four offenders are listed in one of them.
    if name in getattr(server, "_UNCONFINED_ROOT_TOOLS", frozenset()):
        return "ask"
    read_only = getattr(server, "REPOSITORY_READ_ONLY_TOOLS", frozenset())
    inspection = getattr(server, "_WORK_INSPECTION_TOOLS", frozenset())
    if name in _READ_ONLY or name in read_only or name in inspection:
        return "safe"
    return "ask"


# Severity order for combining several tools into one command's grade. Only
# used to pick a maximum -- the grade itself is always one of these strings.
_RISK_SEVERITY = {"safe": 0, "ask": 1, "mutation": 2, "execution": 2,
                  "dangerous": 3}


def _native_risk(group, tool, hit, server, tools_by_name) -> str:
    """Grade for one native slash command, from what its branch actually calls.

    The rule this replaces graded a native command by *string identity*::

        risk = _risk_for(tool, server) if tool else (declared or "safe")

    where ``tool`` was only non-empty when a slash name and a tool name happened
    to coincide. Every command whose branch calls its tool under a different
    name -- the majority -- therefore fell into the "fronts no tool, so it
    cannot mutate on its own, so it is safe" default. Measured across the real
    catalog: 31 native commands graded below what their branch can reach,
    including ``/setaccount`` and ``/register``, whose tools ``admin_set_account``
    and ``admin_register`` are named in ``_DANGEROUS``. A name-matching
    heuristic was silently overriding an explicit danger marking.

    ``console_tools()`` already answers the real question -- it resolves each
    branch to the tools it calls by walking the source -- and the console
    permission gate has been reading it all along. The gate and the catalog
    were simply answering from different sources, so the gate refused what the
    help surface called safe. This makes the catalog read the same derivation.

    Three inputs, and the grade is the strongest of them:

    * the declared value (``command_registry``), so a human judgement about a
      command that fronts no tool at all -- ``/mcp``, ``/selfmod`` -- still
      stands;
    * the name-matched tool, unchanged, for the commands where it does work;
    * every tool the branch actually calls.

    ...after which a call site that pins a disarming literal
    (``_DISARMING_ARGUMENTS``) is subtracted, which is what stops ``/delete``
    being graded ``dangerous`` for a delete it hard-codes as a dry run.

    This is a derived property on purpose. Command 272 is graded by what its
    branch does the moment it is written; nobody has to remember to add it to a
    table, and a table nobody updates is exactly how ``/setaccount`` came to be
    graded ``safe``.
    """
    # Native commands with no registered tool may still be execution commands
    # (notably /runwindow).  The app must publish the very class the gate will
    # decide on, rather than the stale registry's older four-class label.
    declared = _risk_for(tool, server) if tool else _declared_native_risk(
        str(group[0] if group else "").lstrip("/"), hit
    )

    mapped = console_tools()
    disarmed_map = console_disarmed_tools()
    reached: set = set()
    disarmed: set = set()
    for name in group:
        reached |= set(mapped.get(name, ()))
        disarmed |= set(disarmed_map.get(name, ()))

    # Registered tools only. Stand-in names from `_UNREGISTERED_BRANCH_WORK`
    # ("mcp", "selfmod", "location") are not tools and have no `_risk_for`
    # answer; they are graded by the declared value above. Grading them here
    # via `permission_modes.risk_of` would also recurse straight back into this
    # function, since that resolves a name through this catalog.
    graded = [
        _risk_for(name, server) for name in reached
        if name in tools_by_name and name not in disarmed
    ]

    # A branch whose only tool calls are disarmed is graded as what it can
    # actually do, not as what the tool could do if called differently. The
    # declared value is deliberately NOT a floor here: for `/delete` the
    # declaration is itself the inverse error being corrected.
    if disarmed and not graded:
        return "safe"

    candidates = [declared] + graded
    return max(candidates, key=lambda r: _RISK_SEVERITY.get(r, 3))


_SCHEMA_TYPES = {
    "string": "str", "integer": "int", "number": "num",
    "boolean": "bool", "array": "list", "object": "obj",
}


def _params_from_schema(schema: dict) -> tuple:
    properties = (schema or {}).get("properties") or {}
    required = set((schema or {}).get("required") or [])
    out = []
    for pname, spec in properties.items():
        raw = spec.get("type")
        if isinstance(raw, list):  # {"type": ["string", "null"]}
            raw = next((t for t in raw if t != "null"), "string")
        out.append(Param(
            name=pname,
            type=_SCHEMA_TYPES.get(raw, "str"),
            required=pname in required,
            default=spec.get("default"),
        ))
    # Declaration order, deliberately not sorted required-first: positional
    # binding uses this order, and reordering made "/git_branch feature-x"
    # bind the branch name to the tool's second parameter.
    return tuple(out)


def _summarize(text: str) -> str:
    """First sentence of a docstring, collapsed to one line."""
    body = re.sub(r"\s+", " ", str(text or "")).strip()
    if not body:
        return ""
    match = re.match(r"^(.+?[.!?])(?:\s|$)", body)
    return (match.group(1) if match else body).strip()


@functools.lru_cache(maxsize=1)
def catalog() -> tuple[CatalogCommand, ...]:
    """Every reachable command: native slash commands plus every MCP tool."""
    server = importlib.import_module("server")  # lazy compatibility edge

    commands: list[Command] = []
    claimed: set[str] = set()

    try:
        tools = list(server.mcp._tool_manager.list_tools())
    except Exception as exc:
        # Not swallowed. `risk_of` reads `dangerous` out of this catalog, so
        # continuing with an empty list silently reclassified every dangerous
        # tool as `ask` -- and memoised the answer. Raising leaves nothing
        # cached, and the display callers below turn it into a message.
        raise CatalogUnavailable(
            "the MCP tool registry could not be read: %s" % exc
        ) from exc
    tools_by_name = {t.name: t for t in tools}

    command_registry = importlib.import_module("command_registry")
    legacy = {c["name"]: c for c in command_registry.COMMANDS}

    # 1. Native slash commands, with the tool they front when the names line up.
    for group in _native_groups():
        # The curated name wins; otherwise the one the author wrote first,
        # which is the canonical spelling in every branch in both chains
        # ("/todo", "/task", "/tasks" -- not the shortest, "/task").
        canonical = next((n for n in group if n in legacy), group[0])
        aliases = tuple(n for n in group if n != canonical)
        stem = canonical.lstrip("/")
        tool = next(
            (n.lstrip("/") for n in group if n.lstrip("/") in tools_by_name), "",
        )
        row = tools_by_name.get(tool)
        hit = next((legacy[n] for n in group if n in legacy), None)
        category = _CATEGORY_BY_SLASH.get(canonical)
        if not category and hit:
            raw = hit.get("category", "")
            category = _LEGACY_CATEGORY.get(raw, raw)
        if category not in CATEGORIES:
            category = _category_for(tool or stem)
        schema_params = _params_from_schema(getattr(row, "parameters", {})) if row else ()
        if not schema_params:
            schema_params = _native_params(canonical)
        commands.append(Command(
            name=canonical,
            aliases=aliases,
            tool=tool,
            category=category,
            # A console command that drives no tool cannot mutate on its own --
            # but it can still start a host process. ``/runwindow`` is the live
            # case: it fronts no registered tool, so it took the legacy
            # registry's ``ask`` and shipped that to the app, while
            # ``permission_modes.EXECUTION_COMMANDS`` classes it ``execution``
            # and ``plan`` denies it. The stem carries that, so grade by it
            # before falling back to the registry.
            risk=_native_risk(group, tool, hit, server, tools_by_name),
            summary=_summarize(getattr(row, "description", "")) if row else "",
            params=schema_params,
            native=True,
        ))
        claimed.update(group)
        if tool:
            claimed.add("/" + tool)

    # 2. Every remaining MCP tool, reachable as /<tool_name>.
    for row in tools:
        slash = "/" + row.name
        if slash in claimed:
            continue
        commands.append(Command(
            name=slash,
            aliases=(),
            tool=row.name,
            category=_category_for(row.name),
            risk=_risk_for(row.name, server),
            summary=_summarize(row.description),
            params=_params_from_schema(row.parameters),
            native=False,
        ))

    # 3. Curated summaries for native commands the tool registry cannot
    #    describe: the registry first, then the REPL's own help block.
    from_help = _help_summaries()
    filled = []
    for command in commands:
        if command.summary:
            filled.append(command)
            continue
        hit = next((legacy[n] for n in command.all_names if n in legacy), None)
        summary = hit.get("summary", "") if hit else ""
        if not summary:
            summary = next(
                (from_help[n] for n in command.all_names if n in from_help), "",
            )
        filled.append(command if not summary else Command(
            name=command.name, aliases=command.aliases, tool=command.tool,
            category=command.category, risk=command.risk, summary=summary,
            params=command.params, native=command.native,
        ))
    filled.sort(key=lambda c: (c.category, c.name))
    return tuple(filled)


@functools.lru_cache(maxsize=1)
def http_catalog() -> tuple[CatalogCommand, ...]:
    """Commands the HTTP chat slash dispatcher can actually execute.

    Every registered MCP tool remains reachable over HTTP through
    ``_dispatch_catalogued_tool``. Native commands are different: some exist
    only in the local REPL and previously leaked into ``GET /v1/commands``.
    Keep a native entry only when at least one of its spellings is in the
    served dispatch chain, preserving aliases shared by both surfaces. The
    console catalog is deliberately left unchanged.
    """
    direct = http_native_names()
    commands: list[Command] = []
    for command in catalog():
        if not command.native:
            commands.append(command)
            continue
        spellings = [name for name in command.all_names if name in direct]
        if not spellings:
            continue
        canonical = command.name if command.name in direct else spellings[0]
        aliases = tuple(name for name in spellings if name != canonical)
        commands.append(replace(command, name=canonical, aliases=aliases))
    return tuple(commands)


def reset_cache() -> None:
    """Drop the memoised catalog (used after a live reload adds tools)."""
    catalog.cache_clear()
    http_catalog.cache_clear()
    http_native_names.cache_clear()
    console_tools.cache_clear()
    console_disarmed_tools.cache_clear()
    http_slash_tools.cache_clear()
    _module_level_functions.cache_clear()
    # Was cleared by nothing anywhere. `_help_summaries` parses the HELP block
    # out of `sonder_repl.py`, so a live reload that edited a one-liner kept
    # serving the pre-reload text for the life of the process -- the same
    # stale-cache defect as the other four, one field over.
    _help_summaries.cache_clear()


# --- lookup / search ------------------------------------------------------


def by_name(name: str) -> Optional[CatalogCommand]:
    wanted = str(name or "").strip()
    if not wanted:
        return None
    if not wanted.startswith("/"):
        wanted = "/" + wanted
    wanted = wanted.lower()
    for command in catalog():
        if wanted in (n.lower() for n in command.all_names):
            return command
    return None


def categories() -> dict:
    grouped: dict[str, list] = {key: [] for key in CATEGORIES}
    for command in catalog():
        grouped.setdefault(command.category, []).append(command)
    return {k: v for k, v in grouped.items() if v}


def complete(prefix: str = "", limit: int = 12) -> list[CatalogCommand]:
    """Commands to offer for a partially typed slash line.

    A bare "/" offers the curated popular set; every extra character narrows
    by name first, then aliases, then summary text, so the list converges on
    what was actually typed instead of reordering under the cursor.
    """
    text = str(prefix or "").strip().lower()
    if text.startswith("/"):
        text = text[1:]
    everything = catalog()
    if not text:
        popular = [by_name(n) for n in POPULAR]
        return [c for c in popular if c][:limit]

    exact, prefixed, aliased, contained, described = [], [], [], [], []
    for command in everything:
        stem = command.name.lstrip("/").lower()
        alias_stems = [a.lstrip("/").lower() for a in command.aliases]
        if stem == text:
            exact.append(command)
        elif stem.startswith(text):
            prefixed.append(command)
        elif any(a.startswith(text) for a in alias_stems):
            aliased.append(command)
        elif text in stem:
            contained.append(command)
        elif text in (command.summary or "").lower():
            described.append(command)

    # Within a tier, float the curated console commands above raw tool names.
    # Ordering tiers by catalog order alone buried "/files" under every
    # file_* tool for the query "/fil" -- the popular short command is the
    # one the typist almost certainly meant.
    popular_rank = {name: index for index, name in enumerate(POPULAR)}

    def _rank(command):
        return (
            popular_rank.get(command.name, len(POPULAR)),
            0 if command.native else 1,
            len(command.name),
            command.name,
        )

    ranked = []
    for tier in (exact, prefixed, aliased, contained, described):
        ranked.extend(sorted(tier, key=_rank))
    return ranked[:limit]


# A submitted miss gets one guess that `complete` deliberately will not make.
# 0.75 was picked by measuring, not taste: it is the highest cutoff that still
# recovers every ordinary slip (transposition "mdoel", dropped letter "moel",
# "helo", "dianostics", "securty") while answering nothing at all for a word
# that is simply not a command. Loosening it to 0.6 starts pairing "systm"
# with "asset" and "permisions" with "persona", which is worse than silence:
# a wrong guess costs a second failed attempt, and the user cannot tell a
# guess from a match.
_NEAR_MISS_CUTOFF = 0.75

# Two characters cannot be a typo of anything in particular -- every short
# stem sits within one edit of a dozen others -- so guessing from them would
# only ever produce noise.
_NEAR_MISS_MIN_LEN = 3


def near_misses(text: str, limit: int = 5) -> list:
    """Names close enough to `text` to be worth suggesting after a miss.

    This is deliberately NOT part of ``complete``. ``complete`` runs on every
    keystroke behind the slash menu, where the list must shrink as the query
    grows; a fuzzy tier there would make a more specific query offer *more*
    commands. Here the line has already been submitted and already matched
    nothing, so the only alternative to a guess is a dead end.

    Categories are searched alongside commands because ``/help`` invites the
    reader to type one (``/help <category>``) and a typo there previously came
    back as "no command", naming the wrong kind of thing entirely.

    Returns display-ready strings: ``"/model"`` for a command, a bare
    ``"security"`` for a category. Aliases are matched but reported under the
    command they belong to, so a suggestion is never a second spelling of a
    suggestion already in the list.
    """
    # ``difflib.get_close_matches`` raises for n=0 and a negative slice would
    # silently return suggestions. Treat malformed or non-positive limits as
    # an explicit request for no suggestions.
    try:
        limit = int(limit)
    except (TypeError, ValueError, OverflowError):
        return []
    if limit <= 0:
        return []
    wanted = str(text or "").strip().lower().lstrip("/")
    if len(wanted) < _NEAR_MISS_MIN_LEN:
        return []
    try:
        commands = catalog()
        groups = categories()
    except CatalogUnavailable:
        # The caller is already rendering a failure; a guess is not worth
        # turning that into a second, unrelated one.
        return []

    canonical: dict[str, str] = {}
    for command in commands:
        for name in command.all_names:
            canonical.setdefault(name.lstrip("/").lower(), command.name)
    for key in groups:
        canonical.setdefault(key.lower(), key)

    ordered: list[str] = []
    for stem in difflib.get_close_matches(
        wanted, list(canonical), n=limit * 3, cutoff=_NEAR_MISS_CUTOFF,
    ):
        suggestion = canonical[stem]
        if suggestion not in ordered:
            ordered.append(suggestion)
    return ordered[:limit]


def _did_you_mean(text: str) -> str:
    """The one-line recovery appended to a miss, or "" when nothing is close."""
    guesses = near_misses(text)
    if not guesses:
        return ""
    rendered = ", ".join(
        guess if guess.startswith("/") else "%s (category)" % guess
        for guess in guesses
    )
    return "did you mean: %s" % rendered


# --- rendering ------------------------------------------------------------

# One mark per class the gate decides on. ``safe`` is deliberately blank --
# it is the unremarkable case -- which is also why a missing entry here is
# dangerous: ``.get(risk, " ")`` renders an unknown class exactly like a safe
# read. ``execution`` had no entry and no way to get one until ``_risk_for``
# started emitting it, so ``/build_run`` printed with the same blank mark as
# ``/status``. A drift test pins this table to ``permission_modes._MATRIX``.
_RISK_MARK = {
    "safe": " ", "ask": "?", "mutation": "*", "execution": ">", "dangerous": "!",
}


def _line(command: Command, width: int) -> str:
    return "  %s %-*s  %s" % (
        _RISK_MARK.get(command.risk, " "), width, command.name,
        command.summary or "(no description)",
    )


def help_overview() -> str:
    grouped = categories()
    total = sum(len(v) for v in grouped.values())
    lines = [
        "sonder commands  (%d across %d categories)" % (total, len(grouped)),
        "",
        "  /help <category>   list that category      /help <command>  full usage",
        "  /<text>            match commands as you type",
        "",
    ]
    width = max((len(k) for k in grouped), default=8)
    for key in sorted(grouped):
        lines.append("  %-*s  %2d  %s" % (
            width, key, len(grouped[key]), CATEGORIES.get(key, ""),
        ))
    popular = [c for c in (by_name(n) for n in POPULAR) if c]
    if popular:
        lines += ["", "most used"]
        pwidth = max(len(c.name) for c in popular)
        lines += [_line(c, pwidth) for c in popular]
    lines += ["", "  legend: ? asks first   * changes files   "
                  "> runs a program   ! destructive"]
    return "\n".join(lines)


def help_category(name: str) -> str:
    key = str(name or "").strip().lower().lstrip("/")
    grouped = categories()
    if key not in grouped:
        close = [k for k in grouped if k.startswith(key) or key in k]
        if len(close) == 1:
            key = close[0]
        else:
            known = ", ".join(sorted(grouped))
            return "no command category '%s'.\ncategories: %s" % (name, known)
    rows = sorted(grouped[key], key=lambda c: c.name)
    width = max(len(c.name) for c in rows)
    lines = ["%s -- %s  (%d)" % (key, CATEGORIES.get(key, ""), len(rows)), ""]
    for command in rows:
        lines.append(_line(command, width))
        if command.aliases:
            lines.append("    %s  aliases: %s" % (
                " " * width, " ".join(sorted(command.aliases)),
            ))
    return "\n".join(lines)


def help_command(name: str) -> str:
    """Full usage for one command, or the closest matches.

    Guarded rather than left to raise: both ``server.control_command`` and
    ``sonder_repl._run_catalogued`` call this from inside an
    ``except TypeError`` to explain how a tool should have been invoked. A
    blind registry raising here would replace the caller's real mistake with
    an unrelated error about the catalog, on a path whose whole job is telling
    the caller what they got wrong.
    """
    try:
        return _help_command(name)
    except CatalogUnavailable as exc:
        return _UNAVAILABLE_TEXT % exc


def _help_command(name: str) -> str:
    command = by_name(name)
    if not command:
        matches = complete(name, limit=8)
        if not matches:
            miss = "no command '%s'. try /help for categories." % name
            recovery = _did_you_mean(name)
            return "%s\n%s" % (miss, recovery) if recovery else miss
        lines = ["no exact command '%s'. did you mean:" % name, ""]
        width = max(len(c.name) for c in matches)
        return "\n".join(lines + [_line(c, width) for c in matches])
    lines = [
        command.usage(),
        "",
        "  %s" % (command.summary or "(no description)"),
        "",
        "  category: %s    risk: %s" % (command.category, command.risk),
    ]
    if command.aliases:
        lines.append("  aliases:  %s" % " ".join(sorted(command.aliases)))
    if command.tool:
        lines.append("  tool:     %s" % command.tool)
    if command.params:
        lines += ["", "  parameters"]
        width = max(len(p.name) for p in command.params)
        for param in command.params:
            flag = "required" if param.required else "optional"
            default = "" if param.default in (None, "") else "  (default %s)" % param.default
            lines.append("    %-*s  %-5s %s%s" % (
                width, param.name, param.type, flag, default,
            ))
        lines += [
            "",
            "  call it as:  %s %s" % (
                command.name,
                " ".join("%s=<%s>" % (p.name, p.type) for p in command.params[:3]),
            ),
        ]
    return "\n".join(lines)


_UNAVAILABLE_TEXT = (
    "the command catalog is unavailable: %s\n"
    "  nothing can be listed, and the permission gate is refusing commands "
    "until the tool registry answers again."
)


def help_text(topic: str = "") -> str:
    """Dispatcher behind /help: no topic -> categories, else category or command.

    A registry that cannot be read is reported rather than raised: this is the
    display path, and a traceback out of ``/help`` would take the REPL loop
    with it. The enforcing paths fail closed instead -- see
    ``CatalogUnavailable``.
    """
    key = str(topic or "").strip()
    try:
        if not key:
            return help_overview()
        if key.lstrip("/").lower() in categories():
            return help_category(key)
        return help_command(key)
    except CatalogUnavailable as exc:
        return _UNAVAILABLE_TEXT % exc


def format_matches(prefix: str, limit: int = 12) -> str:
    try:
        matches = complete(prefix, limit=limit)
    except CatalogUnavailable as exc:
        return _UNAVAILABLE_TEXT % exc
    if not matches:
        miss = "no command matches '%s'. /help lists every category." % prefix
        recovery = _did_you_mean(prefix)
        return "%s\n%s" % (miss, recovery) if recovery else miss
    header = "most used" if not str(prefix or "").strip("/ ") else (
        "commands matching '%s'" % prefix
    )
    width = max(len(c.name) for c in matches)
    return "\n".join([header] + [_line(c, width) for c in matches])


# --- invocation -----------------------------------------------------------


class InvocationError(ValueError):
    """A structured account of a malformed ``/tool key=value`` line.

    Subclasses ``ValueError`` so every existing ``except ValueError`` display
    path keeps working unchanged; the attributes exist so programmatic
    surfaces (HTTP, MCP, tests) can act on *what* failed without parsing the
    message text.

    ``problem`` is one of ``"unknown-parameter"``, ``"conflicting-duplicate"``,
    or ``"invalid-value"``; ``command`` is the catalogued slash name; and
    ``details`` carries the problem-specific evidence (the unknown keys, the
    duplicated key and both raw values, or the key/raw-value/expected-type
    triple).
    """

    def __init__(self, message, *, command="", problem="", details=None):
        super().__init__(message)
        self.command = command
        self.problem = problem
        self.details = dict(details or {})


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}

# A quoted value keeps its spaces; an unquoted one runs to whitespace, which
# leaves JSON payloads such as steps=["a","b"] intact.
_KV_TOKEN = re.compile(
    r"""\b([A-Za-z_]\w*)=("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|\S+)"""
)

# Scope and plumbing parameters. Many tools declare these first, so a bare
# "/git_branch feature-x" would otherwise bind the branch name to `root`.
_CONTEXT_PARAMS = frozenset({
    "root", "cwd", "project", "token", "approval", "extra_roots", "timeout",
    "max_bytes", "limit", "owner", "priority", "session",
})


def _coerce(value: str, kind: str):
    if kind == "int":
        try:
            return int(value)
        except ValueError:
            return value
    if kind == "num":
        try:
            return float(value)
        except ValueError:
            return value
    if kind == "bool":
        low = value.strip().lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
        return value
    return value


def parse_invocation(line: str):
    """Turn ``/tool a=1 rest of text`` into ``(tool_name, kwargs)``.

    ``key=value`` tokens bind by name; anything left over fills the parameters
    in declaration order, so both ``/file_read path=x`` and ``/file_read x``
    work. Returns None when the line is not a catalogued tool command.

    Raises ``InvocationError`` (a ``ValueError``) for a malformed line rather
    than repairing it silently, because each silent repair ran something other
    than what was typed while looking bounded and deliberate:

    * a ``key=value`` whose key the tool does not accept -- ``/file_read
      path=x limit=5`` would run against the whole file;
    * the same key given twice with different values -- last-wins made
      ``/file_read path=a path=b`` read ``b`` while the line still showed
      ``a``;
    * a named value that cannot be its parameter's declared int/num/bool --
      coercion used to fall back to the raw string, so ``dry_run=nope``
      reached the tool as a *truthy* string and a typo'd flag silently meant
      the opposite of what it said.

    Positional words keep the historical lenient coercion: they carry no
    stated key=type intent, and free-text parameters legitimately absorb
    arbitrary words.
    """
    text = str(line or "").strip()
    if not text.startswith("/"):
        return None
    head, _, rest = text.partition(" ")
    command = by_name(head)
    if not command or not command.tool:
        return None
    by_param = {p.name: p for p in command.params}
    kwargs: dict = {}
    unknown: list[str] = []
    raw_named: dict = {}
    conflicts: dict = {}
    mistyped: dict = {}

    def _take(match):
        key, value = match.group(1), match.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key in by_param:
            if key in raw_named and raw_named[key] != value:
                conflicts[key] = (raw_named[key], value)
            raw_named[key] = value
            kind = by_param[key].type
            coerced = _coerce(value, kind)
            if kind in ("int", "num", "bool") and isinstance(coerced, str):
                mistyped[key] = (value, kind)
            kwargs[key] = coerced
        else:
            unknown.append(key)
        return " "

    # Lift key=value off the raw remainder before any shlex pass: shlex strips
    # the quotes that JSON-valued parameters (steps, items_json) depend on.
    leftover = _KV_TOKEN.sub(_take, rest).strip()
    if unknown:
        raise InvocationError(
            "%s does not take %s. parameters: %s" % (
                command.name,
                ", ".join(sorted(set(unknown))),
                ", ".join(p.name for p in command.params) or "(none)",
            ),
            command=command.name,
            problem="unknown-parameter",
            details={
                "unknown": sorted(set(unknown)),
                "parameters": [p.name for p in command.params],
            },
        )
    if conflicts:
        key = sorted(conflicts)[0]
        first, second = conflicts[key]
        raise InvocationError(
            "%s was given %s twice with different values (%r, then %r). "
            "state it once." % (command.name, key, first, second),
            command=command.name,
            problem="conflicting-duplicate",
            details={"key": key, "values": [first, second]},
        )
    if mistyped:
        key = sorted(mistyped)[0]
        value, kind = mistyped[key]
        raise InvocationError(
            "%s: %s expects %s, got %r." % (command.name, key, kind, value),
            command=command.name,
            problem="invalid-value",
            details={"key": key, "value": value, "expected": kind},
        )

    if leftover:
        candidates = [p for p in command.params if p.name not in kwargs]
        required = [p for p in candidates if p.required]
        open_params = required or (
            [p for p in candidates if p.name not in _CONTEXT_PARAMS] or candidates
        )
        if open_params:
            try:
                words = shlex.split(leftover)
            except ValueError:
                words = leftover.split()
            # One free-text parameter takes the whole remainder rather than
            # only its first whitespace-delimited word.
            if len(open_params) == 1 and open_params[0].type == "str":
                kwargs[open_params[0].name] = leftover
            else:
                for param, value in zip(open_params, words):
                    kwargs[param.name] = _coerce(value, param.type)
    return command.tool, kwargs
