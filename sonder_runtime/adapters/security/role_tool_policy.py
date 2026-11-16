"""Adapter for the role tool policy: the shipped allowlist and the caller role.

``domain/public_policy.py`` decides; this module reads the allowlist data file
and carries the caller's role through the request, so the agent dispatcher
(which never sees an HTTP context) can apply the same decision.

Failure is closed: an unreadable or invalid allowlist is an empty allowlist,
so a user-role account then runs no tool at all.

State: the allowlist is a process-wide immutable frozenset (loaded once). The
caller role is a ``ContextVar`` bound by the HTTP handler thread for the
request it serves; ``None`` means "no role restriction applies" (local-open,
the trusted MCP/console surface, a developer or admin caller).
"""
from __future__ import annotations

import contextvars
import functools
import json
import logging
from pathlib import Path

from sonder_runtime.domain import public_policy
from sonder_runtime.domain.cloud_access import LEGACY_ERROR_PREFIX

_LOG = logging.getLogger(__name__)

ALLOWLIST_PATH = Path(__file__).resolve().parents[1] / "data" / "user_tool_allowlist.json"

_CALLER_ROLE = contextvars.ContextVar("sonder_caller_role", default=None)


@functools.lru_cache(maxsize=1)
def user_tool_allowlist() -> frozenset:
    try:
        payload = json.loads(ALLOWLIST_PATH.read_text(encoding="utf-8"))
        return public_policy.parse_allowlist(payload)
    except (OSError, ValueError):
        _LOG.error("user tool allowlist unavailable; user role runs no tool", exc_info=True)
        return frozenset()


@functools.lru_cache(maxsize=1)
def user_untooled_commands() -> frozenset:
    try:
        payload = json.loads(ALLOWLIST_PATH.read_text(encoding="utf-8"))
        return public_policy.parse_untooled_allowlist(payload)
    except (OSError, ValueError):
        _LOG.error("untooled command allowlist unavailable; user role runs none", exc_info=True)
        return frozenset()


def untooled_refusal_reason(command, role) -> str:
    """"" when ``role`` may run the tool-less slash ``command``, else why not."""
    decision = public_policy.decide_tool(str(command or ""), role, user_untooled_commands())
    return decision.reason if isinstance(decision, public_policy.ToolRefused) else ""


def refusal_reason(tool, role) -> str:
    """"" when ``role`` may run ``tool``, else the refusal reason."""
    decision = public_policy.decide_tool(str(tool or ""), role, user_tool_allowlist())
    return decision.reason if isinstance(decision, public_policy.ToolRefused) else ""


def bind_caller_role(role) -> None:
    """Declare the role the current handler thread serves (None: unrestricted)."""
    _CALLER_ROLE.set(role)


def agent_refusal(tool) -> str:
    """The agent loop's HOST POLICY observation for a restricted caller, or ""."""
    role = _CALLER_ROLE.get()
    if role is None:
        return ""
    reason = refusal_reason(tool, role)
    if not reason:
        return ""
    return "%s HOST POLICY: %s" % (LEGACY_ERROR_PREFIX, reason)
