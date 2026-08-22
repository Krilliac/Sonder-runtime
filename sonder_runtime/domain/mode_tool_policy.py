"""Provider-neutral Chat/Plan/Agent tool availability projection."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from collections.abc import Iterable, Mapping


class AgentMode(StrEnum):
    CHAT = "chat"
    PLAN = "plan"
    AGENT = "agent"


class ToolDisposition(StrEnum):
    EXCLUDED = "excluded"
    AUTOMATIC = "automatic"
    APPROVAL_REQUIRED = "approval_required"


@dataclass(frozen=True, slots=True)
class ModeToolPolicy:
    mode: AgentMode
    dispositions: Mapping[str, ToolDisposition]

    def disposition(self, tool: str) -> ToolDisposition:
        return self.dispositions.get(tool, ToolDisposition.EXCLUDED)

    def available_tools(self) -> tuple[str, ...]:
        return tuple(sorted(
            name for name, disposition in self.dispositions.items()
            if disposition is not ToolDisposition.EXCLUDED
        ))

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode.value,
            "tools": {
                name: self.dispositions[name].value
                for name in sorted(self.dispositions)
            },
        }


def project_mode_tool_policy(
    mode: AgentMode | str,
    tools: Iterable[str],
    *,
    read_only_tools: Iterable[str] = (),
    mutating_tools: Iterable[str] = (),
) -> ModeToolPolicy:
    """Build an inspectable mode policy without executing or classifying tools."""
    try:
        selected = mode if isinstance(mode, AgentMode) else AgentMode(mode)
    except (TypeError, ValueError) as exc:
        raise ValueError("mode must be chat, plan, or agent") from exc
    tool_names = _names(tools, "tools")
    read_only = _names(read_only_tools, "read_only_tools")
    mutating = _names(mutating_tools, "mutating_tools")
    if read_only & mutating:
        raise ValueError("a tool cannot be both read-only and mutating")
    if not read_only | mutating >= tool_names:
        raise ValueError("every tool must be classified read-only or mutating")
    dispositions = {}
    for tool in sorted(tool_names):
        if selected is AgentMode.CHAT:
            disposition = ToolDisposition.EXCLUDED
        elif selected is AgentMode.PLAN:
            disposition = ToolDisposition.AUTOMATIC if tool in read_only else ToolDisposition.EXCLUDED
        else:
            disposition = (
                ToolDisposition.AUTOMATIC if tool in read_only
                else ToolDisposition.APPROVAL_REQUIRED
            )
        dispositions[tool] = disposition
    return ModeToolPolicy(selected, dispositions)


def _names(values: Iterable[str], label: str) -> frozenset[str]:
    try:
        result = frozenset(values)
    except TypeError as exc:
        raise ValueError(f"{label} must be an iterable of names") from exc
    if any(not isinstance(value, str) or not value.strip() for value in result):
        raise ValueError(f"{label} contains an invalid tool name")
    return result


__all__ = [
    "AgentMode", "ModeToolPolicy", "ToolDisposition", "project_mode_tool_policy",
]
