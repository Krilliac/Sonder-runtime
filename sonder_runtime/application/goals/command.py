"""The ``/goal`` slash command as a pure application use case.

Moved out of ``server._goal_command``.  Every collaborator is injected through
:class:`GoalCommandPorts`, so this module imports nothing outside the
application layer and the standard library.  Behaviour is unchanged except for
two fixes found by static audit:

* ``adopt`` rendered its result with an undefined ``_fmt`` and raised
  ``NameError`` on every successful adoption; it now uses ``format_goal``.
* ``set --auto`` never received the caller's project (it tested
  ``'project' in dir()`` for a name that was never bound), so every
  goal-launched autopilot run was unscoped.  The dispatcher's project is now
  threaded through and resolved exactly as ``/autopilot`` and ``/mission``
  resolve theirs.

Slash commands originate only from the user's own chat input, so this layer is
authorized to pass ``actor="user"``; the goal store independently enforces
that closure and adoption can never come from model output.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

GOAL_USAGE = (
    "usage: /goal [show|set <objective> [--criteria a; b]|"
    "note <text>|done [reason]|abandon [reason]|refresh|"
    "proposals|adopt <id>|decline <id>|history]"
)
SET_USAGE = (
    "usage: /goal set [--auto] [--plan] <objective> [--criteria a; b; c]"
)


class GoalCommandFailed(Exception):
    """A user-visible ``/goal`` failure.

    Raised instead of returning an ``ERROR:``-prefixed string: rendering the
    legacy text signal is the host surface's job, which keeps this use case
    typed and out of the legacy error-signal inventory.
    """


@dataclass(frozen=True)
class GoalCommandPorts:
    """Collaborators the host composition root supplies.

    ``goal_store`` is the durable goal store (the root ``goal_store`` module
    today); it must expose ``GoalError`` and the read/write operations used
    below.  ``resolve_project`` follows the session convention: ``""`` maps to
    the default project and ``"none"`` to ``None``.
    """

    goal_store: Any
    format_goal: Callable[[Mapping | None], str]
    goal_to_plan: Callable[[Mapping], Mapping]
    goal_to_autopilot: Callable[..., Mapping]
    launch_autopilot: Callable[[str], object]
    refresh_proposals: Callable[[], Mapping]
    resolve_project: Callable[[str], str | None]


def _parse_set_options(rest: str):
    """Split ``set`` arguments into (auto, plan, objective, criteria, error)."""
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
            return auto, plan, "", "", "unknown goal option '%s'." % option
        tokens = tokens.strip()
    objective, _, criteria = tokens.partition("--criteria")
    return auto, plan, objective.strip(), criteria.strip(), ""


def _set_goal(ports: GoalCommandPorts, rest: str, project: str, request_owner: str) -> str:
    auto, plan, objective, criteria, error = _parse_set_options(rest)
    if error:
        return error
    if not objective:
        return SET_USAGE
    goal = ports.goal_store.set_goal(objective, criteria, origin="user")
    lines = ["goal set", ports.format_goal(goal)]
    if plan and goal.get("criteria"):
        plan_result = ports.goal_to_plan(goal)
        if plan_result.get("error"):
            lines.append("plan: %s" % plan_result["error"])
        else:
            lines.append("plan: %d steps decomposed" % plan_result["step_count"])
    if auto:
        run = ports.goal_to_autopilot(
            goal,
            project=ports.resolve_project(project) or "",
            request_owner=request_owner,
        )
        if run.get("error"):
            lines.append("autopilot: %s" % run["error"])
        else:
            ports.launch_autopilot(run["run_id"])
            lines.append(
                "autopilot: %s started (use /autopilot status %s)"
                % (run["run_id"], run["run_id"])
            )
    return "\n".join(lines)


def _proposals(ports: GoalCommandPorts) -> str:
    rows = ports.goal_store.proposals()
    if not rows:
        return "no pending goal proposals"
    listing = "\n".join("%s  %s" % (row["id"], row["objective"][:120]) for row in rows)
    return listing + (
        "\n(adopt with /goal adopt <id>; dismiss with /goal decline <id>)"
    )


def _history(ports: GoalCommandPorts) -> str:
    rows = ports.goal_store.history()
    if not rows:
        return "no closed goals"
    return "\n".join(
        "%s [%s] %s" % (row["id"], row["status"], row["objective"][:100])
        for row in rows
    )


def _refresh(ports: GoalCommandPorts) -> str:
    result = ports.refresh_proposals()
    if result.get("error"):
        raise GoalCommandFailed(str(result["error"]))
    return (
        "goal proposals refreshed: %d new, %d skipped "
        "(review with /goal proposals)" % (result["proposed"], result["skipped"])
    )


def run_goal_command(
    ports: GoalCommandPorts,
    arg: str,
    *,
    project: str = "",
    request_owner: str = "",
) -> str:
    """Execute one ``/goal`` invocation and return its user-facing text.

    Raises :class:`GoalCommandFailed` for store errors and failed proposal
    refreshes.

    A bare ``/goal`` is the read-only ``show`` form; the permission narrowing
    table in the command catalog relies on that default.
    """
    text = str(arg or "show").strip() or "show"
    action, _, rest = text.partition(" ")
    action = action.lower()
    rest = rest.strip()
    store = ports.goal_store
    try:
        if action in ("show", "status"):
            return ports.format_goal(store.get_active())
        if action == "set":
            return _set_goal(ports, rest, project, request_owner)
        if action == "note":
            if not rest:
                return "usage: /goal note <progress note>"
            return "noted\n" + ports.format_goal(store.add_note(rest))
        if action in ("done", "complete"):
            goal = store.complete(rest, actor="user")
            return "goal completed: %s" % goal["objective"]
        if action in ("abandon", "drop"):
            goal = store.abandon(rest, actor="user")
            return "goal abandoned: %s" % goal["objective"]
        if action == "refresh":
            return _refresh(ports)
        if action == "proposals":
            return _proposals(ports)
        if action == "adopt":
            return "adopted\n" + ports.format_goal(store.adopt(rest, actor="user"))
        if action == "decline":
            goal = store.decline(rest, actor="user")
            return "declined proposal %s" % goal["id"]
        if action == "history":
            return _history(ports)
        return GOAL_USAGE
    except store.GoalError as exc:
        raise GoalCommandFailed(str(exc)) from exc
