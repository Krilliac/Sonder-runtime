"""Role tool policy: which tools an account role may run (public-mode slice P1).

Deny by default. A role above ``user`` (developer, admin) keeps today's
behaviour exactly: the process-wide permission mode and the system-operation
bindings decide. A ``user`` role, and any role this module does not know, may
run only the tools on the explicit allowlist the operator ships as data.

I/O-free: the allowlist is passed in as a frozenset, never read here.
``parse_allowlist`` validates the decoded data file; reading it is the
adapter's job (``adapters/security/role_tool_policy.py``).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Union

ROLE_USER = "user"
ROLE_DEVELOPER = "developer"
ROLE_ADMIN = "admin"

# Roles whose tool authority is unchanged by this policy.
UNRESTRICTED_ROLES = frozenset({ROLE_DEVELOPER, ROLE_ADMIN})

ALLOWLIST_SCHEMA_VERSION = 1
KEY_SCHEMA_VERSION = "schema_version"
KEY_TOOLS = "tools"
KEY_NAME = "name"
KEY_WHY = "why"

REFUSAL_TEMPLATE = "tool '%s' is not available to this account role"


@dataclass(frozen=True)
class ToolAllowed:
    """The role policy has no objection (the permission mode still decides)."""


@dataclass(frozen=True)
class ToolRefused:
    tool: str
    role: str

    @property
    def reason(self) -> str:
        return REFUSAL_TEMPLATE % self.tool


ToolDecision = Union[ToolAllowed, ToolRefused]


def decide_tool(tool: str, role: str, user_allowlist: frozenset) -> ToolDecision:
    """Allow an unrestricted role, or any role for a tool on the allowlist."""
    if role in UNRESTRICTED_ROLES:
        return ToolAllowed()
    if tool in user_allowlist:
        return ToolAllowed()
    return ToolRefused(tool=str(tool), role=str(role))


def parse_allowlist(payload) -> frozenset:
    """The allowed tool names from a decoded allowlist document.

    Raises ValueError for any shape problem so the caller can fail closed.
    Every entry must carry a ``why``: an allowlisted tool without a stated
    reason is a review failure, not a default.
    """
    if not isinstance(payload, dict):
        raise ValueError("allowlist document must be an object")
    if payload.get(KEY_SCHEMA_VERSION) != ALLOWLIST_SCHEMA_VERSION:
        raise ValueError("unsupported allowlist schema version")
    entries = payload.get(KEY_TOOLS)
    if not isinstance(entries, list):
        raise ValueError("allowlist 'tools' must be a list")
    names = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("allowlist entries must be objects")
        name = entry.get(KEY_NAME)
        why = entry.get(KEY_WHY)
        if not isinstance(name, str) or not name.strip() or name != name.strip():
            raise ValueError("allowlist entry needs an exact tool name")
        if not isinstance(why, str) or not why.strip():
            raise ValueError("allowlist entry '%s' needs a justification" % name)
        names.append(name)
    if len(set(names)) != len(names):
        raise ValueError("allowlist names a tool twice")
    return frozenset(names)
