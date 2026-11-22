"""Tool arguments a slash command calls with."""
from __future__ import annotations

from typing import Any

from sonder_runtime.interfaces.http.facades import developer_chat


def slash_call_arguments(cmd: str, argument: object) -> dict[str, Any] | None:
    """The canonical tool arguments a slash command will call with, or None."""
    return developer_chat.slash_call_arguments(cmd, str(argument or ""))
