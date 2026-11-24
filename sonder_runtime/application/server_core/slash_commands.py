"""Pure parsers, formatters, and policy builders for slash commands."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

SELFMOD_HELP_TEXT = (
    "selfmod: status|opportunities|history|inspect <id>|plan <objective> --files a,b|"
    "run <objective> --files a,b --tests <command> [--unisolated]|diff <id>|tests <id>|approve <id>|"
    "reject <id>|deploy <id>|rollback <id>|backups|verify-backup <id>|"
    "mode observe|propose|auto-low-risk|resume <id>|cancel <id>|retention <days> <GB>|prune-backups|disable|enable"
)

GOAL_USAGE_TEXT = (
    "usage: /goal [show|set <objective> [--criteria a; b]|"
    "note <text>|done [reason]|abandon [reason]|refresh|"
    "proposals|adopt <id>|decline <id>|history]"
)

MISSION_USAGE_TEXT = (
    "usage: /mission [status|start [--auto] [--plan] [--observe] "
    "<objective> [--criteria ...]|done [reason]|abandon [reason]|bindings]"
)

MCP_HELP_TEXT = (
    "MCP runtime commands:\n"
    "  /mcp status\n"
    "  /mcp refresh\n"
    "Updated implementations and tool schemas publish atomically; a bad edit "
    "keeps the last known-good registry."
)

TRAINING_HELP_TEXT = (
    "training commands:\n"
    "  /hardware\n"
    "  /training plan [--dry-run] [--model auto|1.5b|3b|7b]\n"
    "  /training start --confirm [planning options]\n"
    "  /training status\n"
    "  /training deploy [--adapter-dir PATH] [--llama-cpp PATH]\n"
    "  /training adopt-legacy --confirm\n"
    "  /training release-alias --confirm\n"
    "  /training rollback\n"
    "Options: --allow-cpu-offload --max-vram N --max-system-ram N "
    "--context-length N --sequence-length N --batch-size N "
    "--gradient-accumulation N --gpu-index N --resume. "
    "--full-finetune is planning-only; attended start supports QLoRA."
)

AUTOPILOT_HELP_TEXT = (
    "autopilot commands:\n"
    "  /autopilot status [id]\n"
    "  /autopilot plan [--observe] [--no-web] [--static] [--goal] <objective>\n"
    "  /autopilot run [--observe] [--no-web] [--static] [--goal] <objective>\n"
    "  /autopilot resume|pause|cancel <id>\n"
    "  /autopilot steer|clarify <id> <message>   (owner-scoped note; "
    "clarify also requests a pause)\n"
    "  --goal: bind to the active goal (use /goal set first)"
)

RUNTIME_HELP_TEXT = (
    "runtime policy commands:\n"
    "  /runtime status   (cached model readiness; no model call)\n"
    "  /runtime status refresh   (re-check the local model inventory now)\n"
    "  /runtime set fast=<model> code=<model> general=<model>\n"
    "  /runtime set reasoning=<model> vision=<model>   (specialist "
    "tiers; assign an empty value to leave one unset)\n"
    "  /runtime set embedding=<installed-embedding-model>\n"
    "  /runtime set chat=<tier> router=<tier> workbench=<tier> "
    "autopilot=<tier> fleet=<tier> review=<tier>\n"
    "  /runtime overflow [status|on|off|threshold <n>|model <m>]   (long-context overflow)\n"
    "  /runtime tools [allow|revoke|deny|undeny ...]   (per-model tool lists; /runtime tools help)\n"
    "  /runtime models [set <key> <json>|reset <key>]\n  /runtime reset\n"
    "Only installed local models are accepted. Embedding changes affect "
    "future vectors only; use /embeddings apply to refresh stored memory. "
    "Execution lanes route to fast/code/general only."
)


@dataclass(frozen=True)
class AutopilotRunArgs:
    """Parsed arguments for ``/autopilot run|start|plan``."""

    policy: str = "workspace"
    allow_web: bool = True
    adaptive: bool = True
    bind_goal: bool = False
    objective: str = ""
    usage_error: str = ""
    error_detail: str = ""


def parse_autopilot_run_args(action: str, rest: str) -> AutopilotRunArgs:
    """Parse flags and objective for ``/autopilot run|start|plan``."""
    policy = "workspace"
    allow_web = True
    adaptive = True
    bind_goal = False
    tokens = rest
    while tokens.startswith("--"):
        option, _, remaining = tokens.partition(" ")
        if option == "--observe":
            policy = "observe"
        elif option == "--no-web":
            allow_web = False
        elif option == "--static":
            adaptive = False
        elif option == "--goal":
            bind_goal = True
        else:
            return AutopilotRunArgs(error_detail="unknown autopilot option '%s'." % option)
        tokens = remaining.strip()
    if not tokens and not bind_goal:
        return AutopilotRunArgs(
            usage_error=(
                "usage: /autopilot %s [--observe] [--no-web] [--static] "
                "[--goal] <objective>" % action
            )
        )
    return AutopilotRunArgs(
        policy=policy,
        allow_web=allow_web,
        adaptive=adaptive,
        bind_goal=bind_goal,
        objective=tokens,
    )


@dataclass(frozen=True)
class RuntimeSetArgs:
    """Parsed ``key=value`` assignments for ``/runtime set``."""

    local_models: dict[str, str]
    routing: dict[str, str]
    embedding_model: str = ""
    usage_error: str = ""
    error_detail: str = ""


def parse_runtime_set_args(
    rest: str,
    *,
    local_tiers: Any,
    routing_lanes: Any,
) -> RuntimeSetArgs:
    """Parse and categorize ``/runtime set key=value`` assignments."""
    local_models: dict[str, str] = {}
    routing: dict[str, str] = {}
    embedding_model = ""
    for item in rest.split():
        if "=" not in item:
            return RuntimeSetArgs(
                local_models={},
                routing={},
                error_detail="runtime assignment must use key=value: %s" % item,
            )
        key, value = item.split("=", 1)
        key, value = key.strip().lower(), value.strip()
        if key in local_tiers:
            local_models[key] = value
        elif key == "embedding":
            if not value:
                return RuntimeSetArgs(
                    local_models={},
                    routing={},
                    error_detail="embedding model cannot be empty.",
                )
            embedding_model = value
        elif key in routing_lanes:
            routing[key] = value
        else:
            return RuntimeSetArgs(
                local_models={},
                routing={},
                error_detail="unknown runtime policy key '%s'." % key,
            )
    if not local_models and not routing and not embedding_model:
        return RuntimeSetArgs(
            local_models={},
            routing={},
            usage_error=(
                "usage: /runtime set code=<local-model> reasoning=<local-model> "
                "embedding=<local-embedding-model> workbench=<fast|code|general>"
            ),
        )
    return RuntimeSetArgs(
        local_models=local_models,
        routing=routing,
        embedding_model=embedding_model,
    )


@dataclass(frozen=True)
class GoalSetArgs:
    """Parsed arguments for ``/goal set``."""

    auto: bool = False
    plan: bool = False
    objective: str = ""
    criteria: str = ""
    error: str = ""


@dataclass(frozen=True)
class MissionStartArgs:
    """Parsed arguments for ``/mission start``."""

    auto: bool = False
    plan: bool = False
    policy: str = "workspace"
    allow_web: bool = True
    objective: str = ""
    criteria: str = ""
    error: str = ""


def propose_goals_from_issues(
    issues: Sequence[Mapping[str, Any]],
    *,
    propose_fn: Callable[..., bool],
    scope: str = "",
) -> dict[str, Any]:
    """Turn host-observed improvement findings into proposed goals."""
    proposed = 0
    skipped = 0
    for issue in issues:
        severity = str(issue.get("severity") or "").lower()
        title = str(issue.get("title") or "").strip()
        action = str(issue.get("action") or "").strip()
        if severity == "low" or not title:
            skipped += 1
            continue
        objective = "%s: %s" % (issue.get("area", "runtime"), title)
        criteria = [action] if action else []
        criteria.append(
            "the same finding no longer appears in system_improvement_report"
        )
        if propose_fn(objective, criteria, scope=scope, source="self-observed"):
            proposed += 1
        else:
            skipped += 1
    return {"proposed": proposed, "skipped": skipped, "error": ""}


def parse_goal_set_args(rest: str) -> GoalSetArgs:
    """Parse flags, objective, and criteria for ``/goal set``."""
    auto = False
    plan = False
    tokens = rest
    while tokens.startswith("--"):
        option, _, tokens = tokens.partition(" ")
        if option == "--auto":
            auto = True
        elif option == "--plan":
            plan = True
        elif option == "--criteria":
            tokens = "--criteria " + tokens
            break
        else:
            return GoalSetArgs(error="unknown goal option '%s'." % option)
        tokens = tokens.strip()
    objective, _, criteria = tokens.partition("--criteria")
    if not objective.strip():
        return GoalSetArgs(
            error="usage: /goal set [--auto] [--plan] <objective> [--criteria a; b; c]"
        )
    return GoalSetArgs(
        auto=auto,
        plan=plan,
        objective=objective.strip(),
        criteria=criteria.strip(),
    )


def format_goal_set_reply(
    goal: Mapping[str, Any],
    *,
    format_goal: Callable[[Mapping[str, Any]], str],
    plan_result: Mapping[str, Any] | None = None,
    autopilot_result: Mapping[str, Any] | None = None,
    autopilot_launched: bool = False,
    autopilot_not_launched_msg: str = "",
) -> str:
    """Format the multi-line confirmation for ``/goal set``."""
    lines = ["goal set", format_goal(goal)]
    if plan_result is not None:
        if plan_result.get("error"):
            lines.append("plan: %s" % plan_result["error"])
        else:
            lines.append("plan: %d steps decomposed" % plan_result["step_count"])
    if autopilot_result is not None:
        if autopilot_result.get("error"):
            lines.append("autopilot: %s" % autopilot_result["error"])
        elif autopilot_launched:
            run_id = autopilot_result["run_id"]
            lines.append(
                "autopilot: %s started (use /autopilot status %s)" % (run_id, run_id)
            )
        else:
            lines.append("autopilot: %s" % autopilot_not_launched_msg)
    return "\n".join(lines)


def format_goal_proposals(rows: Sequence[Mapping[str, Any]]) -> str:
    """Format pending goal proposals for ``/goal proposals``."""
    if not rows:
        return "no pending goal proposals"
    listing = "\n".join(
        "%s  %s" % (row["id"], str(row["objective"])[:120]) for row in rows
    )
    return listing + (
        "\n(adopt with /goal adopt <id>; dismiss with /goal decline <id>)"
    )


def format_goal_history(rows: Sequence[Mapping[str, Any]]) -> str:
    """Format closed goals for ``/goal history``."""
    if not rows:
        return "no closed goals"
    return "\n".join(
        "%s [%s] %s" % (row["id"], row["status"], str(row["objective"])[:100])
        for row in rows
    )


def parse_mission_start_args(rest: str) -> MissionStartArgs:
    """Parse flags, objective, and criteria for ``/mission start``."""
    auto = False
    plan = False
    policy = "workspace"
    allow_web = True
    tokens = rest
    while tokens.startswith("--"):
        option, _, remaining = tokens.partition(" ")
        if option == "--auto":
            auto = True
        elif option == "--plan":
            plan = True
        elif option == "--observe":
            policy = "observe"
        elif option == "--no-web":
            allow_web = False
        else:
            return MissionStartArgs(error="unknown mission option '%s'." % option)
        tokens = remaining.strip()
    objective, _, criteria = tokens.partition("--criteria")
    if not objective.strip():
        return MissionStartArgs(
            error=(
                "usage: /mission start [--auto] [--plan] [--observe] "
                "[--no-web] <objective> [--criteria a; b; c]"
            )
        )
    return MissionStartArgs(
        auto=auto,
        plan=plan,
        policy=policy,
        allow_web=allow_web,
        objective=objective.strip(),
        criteria=criteria.strip(),
    )


def format_mission_start_reply(
    result: Mapping[str, Any],
    *,
    format_goal: Callable[[Mapping[str, Any]], str],
    launch_autopilot: Callable[[str], bool],
    autopilot_not_launched: Callable[[str], str],
) -> str:
    """Format the output of ``/mission start`` and launch autopilot if created."""
    lines = ["mission started", format_goal(result["goal"])]
    if result.get("plan"):
        p = result["plan"]
        if p.get("error"):
            lines.append("plan: %s" % p["error"])
        else:
            lines.append("plan: %d steps decomposed from criteria" % p["step_count"])
    if result.get("autopilot"):
        ap = result["autopilot"]
        if ap.get("error"):
            lines.append("autopilot: %s" % ap["error"])
        else:
            run_id = str(ap["run_id"])
            lines.append(
                "autopilot: %s created (use /autopilot status %s)" % (run_id, run_id)
            )
            if not launch_autopilot(run_id):
                lines.append("autopilot: %s" % autopilot_not_launched(run_id))
    return "\n".join(lines)


def dispatch_selfmod_basic_action(
    action: str,
    rest: str,
    *,
    selfmod: Any,
    improvement_report_fn: Callable[[], str],
) -> str | None:
    """Handle read-only, configuration, and non-journaled selfmod actions.

    Returns ``None`` for actions that require stage-journal or lease handling
    (``plan``, ``run``, ``approve``, ``deploy``, ``rollback``, or unknown).
    """
    if action in {"status", "show", "list", "history"}:
        return str(selfmod.format_status())
    if action == "opportunities":
        return "Concrete host evidence for proposals:\n\n" + improvement_report_fn()
    if action == "inspect":
        return str(selfmod.format_run(rest))
    if action == "diff":
        return str(selfmod.diff_text(rest) or "(no candidate diff)")
    if action == "tests":
        return json.dumps(selfmod.test_results(rest), indent=2, ensure_ascii=False)
    if action == "backups":
        rows = [run for run in selfmod.list_runs(100) if run.get("backup_manifest")]
        return (
            "\n".join(
                "%s %s %s" % (run["id"], run["phase"], run["backup_manifest"])
                for run in rows
            )
            or "(no backups)"
        )
    if action == "verify-backup":
        manifest = selfmod.verify_backup(rest)
        return "Backup verified: %s (%d file records)" % (rest, len(manifest["files"]))
    if action == "mode":
        if not rest:
            return "selfmod mode: %s" % selfmod.settings()["mode"]
        return "selfmod mode: %s" % selfmod.set_mode(rest)["mode"]
    if action in {"disable", "enable"}:
        return "selfmod enabled: %s" % selfmod.set_enabled(action == "enable")["enabled"]
    if action == "retention":
        values = rest.split()
        if len(values) != 2:
            return "usage: /selfmod retention <days> <max-gb>"
        configured = selfmod.set_retention(
            int(values[0]), int(float(values[1]) * 1024**3)
        )
        return "selfmod retention: %d days, %.2f GB" % (
            configured["retention_days"],
            configured["retention_bytes"] / 1024**3,
        )
    if action == "prune-backups":
        removed = selfmod.prune_backups()
        return "pruned backups: %s" % (", ".join(removed) or "none")
    if action == "resume":
        run = selfmod.resume(rest)
        return str(selfmod.format_run(run["id"]))
    if action == "cancel":
        return str(selfmod.format_run(selfmod.cancel(rest)["id"]))
    if action == "reject":
        run_id, _, reason = rest.partition(" ")
        return str(
            selfmod.format_run(
                selfmod.reject(run_id, reason or "explicit user rejection")["id"]
            )
        )
    if action in {"help", "?"}:
        return SELFMOD_HELP_TEXT
    return None


def parse_selfmod_plan_flags(
    rest: str, unisolated_flag: str
) -> tuple[bool, bool, str]:
    """Strip ``--maintenance`` and unisolated flags from ``/selfmod plan|run`` args."""
    words = rest.split()
    maintenance = "--maintenance" in words
    unisolated = unisolated_flag in words
    parsed_rest = " ".join(
        part for part in words if part not in ("--maintenance", unisolated_flag)
    )
    return maintenance, unisolated, parsed_rest


_SELFMOD_MUTATION_TOOLS = frozenset({"file_write", "file_edit", "file_delete"})
_SELFMOD_PATH_TOOLS = _SELFMOD_MUTATION_TOOLS | frozenset(
    {
        "file_read",
        "file_read_range",
        "file_find",
        "text_search",
        "directory_tree",
        "workspace_inventory",
        "script_search",
    }
)
_SELFMOD_PATH_KEYS: Mapping[str, str] = {
    "file_write": "path",
    "file_edit": "path",
    "file_delete": "path",
    "file_read": "path",
    "file_read_range": "path",
    "directory_tree": "path",
    "workspace_inventory": "path",
    "file_find": "root",
    "text_search": "root",
    "script_search": "root",
}


def build_selfmod_agent_policy(
    run: Mapping[str, Any],
    *,
    reject_network_path: Callable[[str], str],
    network_refusal_label: str,
    error_prefix: str,
) -> Callable[[str, Any], str]:
    """Build the workspace/scope/budget policy closure for a selfmod editing agent."""
    workspace = Path(str(run["workspace_path"])).resolve()
    allowed = {(workspace / str(path)).resolve(strict=False) for path in run["files"]}
    budgets = run["budgets"]
    inspected: set[str] = set()
    counters = {"tools": 0}
    started = time.monotonic()

    def _deny(detail: str) -> str:
        return f"{error_prefix} SELFMOD POLICY: {detail}"

    def policy(tool_name: str, args: Any) -> str:
        if not isinstance(args, dict):
            return _deny("tool arguments must be an object.")
        counters["tools"] += 1
        if counters["tools"] > budgets["max_tool_calls"]:
            return _deny("tool-call budget exhausted.")
        if time.monotonic() - started > budgets["max_runtime_seconds"]:
            return _deny("total runtime budget exhausted.")
        if tool_name == "workspace_run":
            try:
                cwd_text = reject_network_path(str(args.get("cwd") or workspace))
            except PermissionError:
                return _deny(f"the working directory uses a {network_refusal_label}.")
            try:
                cwd = Path(cwd_text).expanduser().resolve(strict=False)
            except (OSError, ValueError):
                return _deny("the working directory could not be checked.")
            if cwd != workspace and workspace not in cwd.parents:
                return _deny("commands must run inside the candidate workspace.")
            command_text = " ".join(
                str(item) for item in (args.get("args_json") or args.get("args") or ())
            )
            if "selfmod" in command_text.lower():
                return _deny("recursive self-improvement is forbidden.")
            return ""
        if tool_name not in _SELFMOD_PATH_TOOLS:
            return ""
        if any(args.get(name) for name in ("token", "approval", "extra_roots")):
            return _deny("filesystem authority cannot be expanded by the candidate.")
        raw = args.get("path") or args.get("root") or args.get("cwd") or ""
        try:
            target = Path(reject_network_path(str(raw))).expanduser()
        except PermissionError:
            return _deny(f"path uses a {network_refusal_label}.")
        if not target.is_absolute():
            target = workspace / target
        try:
            target = target.resolve(strict=False)
        except (OSError, ValueError):
            return _deny("path could not be checked.")
        if target != workspace and workspace not in target.parents:
            return _deny("path is outside the isolated candidate workspace.")
        if tool_name in _SELFMOD_MUTATION_TOOLS and target not in allowed:
            return _deny("mutation is outside the pre-backed-up file scope.")
        if tool_name not in _SELFMOD_MUTATION_TOOLS:
            inspected.add(str(target))
            if len(inspected) > budgets["max_files_inspected"]:
                return _deny("file-inspection budget exhausted.")
        key = _SELFMOD_PATH_KEYS.get(tool_name)
        if key:
            args[key] = str(target)
        args.pop("token", None)
        args.pop("approval", None)
        args.pop("extra_roots", None)
        return ""

    return policy
