"""User-facing orchestration commands shared by native and HTTP chat.

The host supplies execution and capacity functions; this module never imports
the legacy server or owns scheduling. Metadata is created only by this route,
not inferred from model-authored text.
"""
from __future__ import annotations

from dataclasses import dataclass
import re


class CommandReply(str):
    """A text-compatible command answer with additive trusted UI metadata."""

    def __new__(cls, text: str, **receipt_fields):
        reply = super().__new__(cls, text)
        reply.receipt_fields = receipt_fields
        return reply


@dataclass(frozen=True)
class MasterArguments:
    task: str
    mode: str = "ask"
    agents: int = 0


_MODES = {
    "ask": "ask", "choose": "ask", "prompt": "ask",
    "inline": "inline", "master": "inline", "inlne": "inline",
    "delegate": "delegate", "delegated": "delegate", "agents": "delegate",
    "parallel": "delegate", "delagte": "delegate", "delegte": "delegate",
    "paralell": "delegate", "fleet": "fleet", "swarm": "fleet",
    "fanout": "fleet", "workflow": "fleet",
}
USAGE = "usage: /master_orchestrate [inline|delegate|fleet] [N] <task> (0 means auto)"


def uses_tool_arguments(argument: str) -> bool:
    """Preserve the existing JSON and key=value tool invocation surfaces."""
    text = argument.lstrip()
    return text.startswith("{") or bool(re.match(
        r"(?:task|mode|agents|worker_cap|tier|learn|retry_of|project)=", text,
    ))


def parse_master_arguments(argument: str) -> MasterArguments:
    text = argument.strip()
    parts = text.split(None, 1)
    mode = "ask"
    if parts and parts[0].lower() in _MODES:
        mode = _MODES[parts[0].lower()]
        text = parts[1].strip() if len(parts) > 1 else ""
    parts = text.split(None, 1)
    agents = 0
    if parts and re.fullmatch(r"[+-]?\d+", parts[0]):
        agents = int(parts[0])
        if agents < 0:
            raise ValueError("Agent count must be zero (auto) or positive.")
        text = parts[1].strip() if len(parts) > 1 else ""
    if not text:
        raise ValueError(USAGE)
    return MasterArguments(task=text, mode=mode, agents=agents)


def master_choice(task: str, delegate_agents: int, fleet_agents: int,
                  worker_slots: int) -> CommandReply:
    choices = [
        {"label": "Inline", "command": f"/master_orchestrate inline 0 {task}"},
        {"label": f"{delegate_agents} agents",
         "command": f"/master_orchestrate delegate {delegate_agents} {task}"},
        {"label": f"Fleet of {fleet_agents}",
         "command": f"/master_orchestrate fleet {fleet_agents} {task}"},
    ]
    return CommandReply(
        f"I can do this inline, with {delegate_agents} agents, or as a fleet of "
        f"{fleet_agents} agents on {worker_slots} worker slots — which?",
        orchestration={"task": task, "choices": choices,
                       "delegate_agents": delegate_agents,
                       "fleet_agents": fleet_agents, "worker_slots": worker_slots},
    )


def execute_master_command(argument: str, *, orchestrate, capacity,
                           project: str = "") -> str:
    try:
        parsed = parse_master_arguments(argument)
    except ValueError as error:
        return str(error)
    count = parsed.agents
    if not count and parsed.mode in {"delegate", "fleet"}:
        # A zero/missing CLI count is an automatic *capacity-sized* wave.
        # Never pass zero to an older host whose zero means maximum breadth.
        count = max(1, int(capacity().get("worker_slots") or 1))
    return orchestrate(task=parsed.task, mode=parsed.mode, agents=count,
                       project=project)


def reply_receipt_fields(reply) -> dict:
    return dict(reply.receipt_fields) if isinstance(reply, CommandReply) else {}
