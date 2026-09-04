"""Declarative tool governance: policy-as-code for tool permissions.

Evaluates YAML-defined policies to produce deterministic allow/deny/require_approval
verdicts before any tool executes.

Pure domain logic -- no I/O beyond loading a YAML file via classmethod.
The engine is immutable after construction; policies are sorted once by
descending priority and evaluated first-match-wins.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from fnmatch import fnmatch
from typing import Any


class Verdict(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class GovernanceInputError(ValueError):
    """Raised when a policy definition is malformed."""


def _parse_verdict(raw: Any) -> Verdict:
    if isinstance(raw, Verdict):
        return raw
    if not isinstance(raw, str):
        raise GovernanceInputError(f"verdict must be a string, got {type(raw).__name__}")
    try:
        return Verdict(raw)
    except ValueError as exc:
        raise GovernanceInputError(
            f"invalid verdict {raw!r}; expected one of {[v.value for v in Verdict]}"
        ) from exc


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    """A single governance rule matching tool names to a verdict."""

    name: str
    tools: tuple[str, ...]
    verdict: Verdict
    conditions: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    priority: int = 0

    def __post_init__(self) -> None:
        if not self.name or not isinstance(self.name, str):
            raise GovernanceInputError("policy name is required")
        if not self.tools:
            raise GovernanceInputError(f"policy {self.name!r} must specify at least one tool pattern")
        if not isinstance(self.priority, int) or isinstance(self.priority, bool):
            raise GovernanceInputError(f"priority must be an integer in policy {self.name!r}")
        # Normalize verdict if passed as raw string.
        object.__setattr__(self, "verdict", _parse_verdict(self.verdict))

    def matches_tool(self, tool_name: str) -> bool:
        """Return True if *tool_name* matches any pattern in this policy."""
        return any(fnmatch(tool_name, pattern) for pattern in self.tools)

    def matches_conditions(self, context: dict[str, Any] | None) -> bool:
        """Return True if the optional conditions are satisfied by *context*."""
        if not self.conditions:
            return True
        if context is None:
            return False
        for key, value in self.conditions.items():
            if key == "path_prefix":
                ctx_path = context.get("path", "")
                if not isinstance(ctx_path, str) or not ctx_path.startswith(value):
                    return False
            else:
                if context.get(key) != value:
                    return False
        return True


class PolicyEngine:
    """Evaluate an ordered list of tool policies to produce a verdict.

    Policies are sorted by descending priority at construction time.
    On ``evaluate``, the first matching policy wins.  When no policy
    matches, the default verdict is ``DENY`` -- explicit allowlisting
    is required.
    """

    _DEFAULT_REASON = "no policy matched; default deny"

    def __init__(self, policies: list[ToolPolicy]) -> None:
        if any(not isinstance(p, ToolPolicy) for p in policies):
            raise GovernanceInputError("all entries must be ToolPolicy instances")
        self._policies: tuple[ToolPolicy, ...] = tuple(
            sorted(policies, key=lambda p: (-p.priority, p.name))
        )

    @property
    def policies(self) -> tuple[ToolPolicy, ...]:
        return self._policies

    def evaluate(
        self, tool_name: str, context: dict[str, Any] | None = None
    ) -> tuple[Verdict, str]:
        """Return ``(verdict, reason)`` for *tool_name* under *context*."""
        for policy in self._policies:
            if policy.matches_tool(tool_name) and policy.matches_conditions(context):
                reason = policy.reason or f"matched policy {policy.name!r}"
                return policy.verdict, reason
        return Verdict.DENY, self._DEFAULT_REASON

    # ------------------------------------------------------------------
    # Structured loaders
    # ------------------------------------------------------------------

    @classmethod
    def load_from_dict(cls, data: dict) -> PolicyEngine:
        """Load policies from a pre-parsed dictionary.

        Accepts the structure that ``yaml.safe_load`` or ``json.loads``
        would produce.  YAML/JSON parsing is the caller's responsibility
        so this module stays free of third-party imports.
        """
        if not isinstance(data, dict) or "policies" not in data:
            raise GovernanceInputError("data must contain a top-level 'policies' key")
        raw_policies = data["policies"]
        if not isinstance(raw_policies, list):
            raise GovernanceInputError("'policies' must be a list")
        policies: list[ToolPolicy] = []
        for entry in raw_policies:
            if not isinstance(entry, dict):
                raise GovernanceInputError("each policy entry must be a mapping")
            tools_raw = entry.get("tools", [])
            if isinstance(tools_raw, str):
                tools_raw = [tools_raw]
            policies.append(
                ToolPolicy(
                    name=entry.get("name", ""),
                    tools=tuple(tools_raw),
                    verdict=_parse_verdict(entry.get("verdict", "deny")),
                    conditions=entry.get("conditions") or {},
                    reason=entry.get("reason", ""),
                    priority=entry.get("priority", 0),
                )
            )
        return cls(policies)


__all__ = [
    "GovernanceInputError",
    "PolicyEngine",
    "ToolPolicy",
    "Verdict",
]
