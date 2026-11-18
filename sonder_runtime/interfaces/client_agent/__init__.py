"""Lightweight local client companion for ``execution_target="client"`` tools."""
from __future__ import annotations

from .executor import (
    CLIENT_AGENT_MODES,
    CLIENT_DELEGATABLE_TOOLS,
    ClientAgentPolicy,
    ClientToolExecutionResult,
    execute_client_tool,
)
from .server import (
    DEFAULT_CLIENT_AGENT_HOST,
    DEFAULT_CLIENT_AGENT_PORT,
    ClientAgentServer,
    create_client_agent_server,
)

__all__ = [
    "CLIENT_AGENT_MODES",
    "CLIENT_DELEGATABLE_TOOLS",
    "ClientAgentPolicy",
    "ClientAgentServer",
    "ClientToolExecutionResult",
    "DEFAULT_CLIENT_AGENT_HOST",
    "DEFAULT_CLIENT_AGENT_PORT",
    "create_client_agent_server",
    "execute_client_tool",
]
