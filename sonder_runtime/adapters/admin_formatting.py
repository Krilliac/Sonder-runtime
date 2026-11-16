"""Stable human-readable rendering for hosted account records."""
from __future__ import annotations

from typing import Any


def _format_account(account: dict[str, Any]) -> str:
    return (
        "%(username)s role=%(role)s tier=%(tier)s banned=%(banned)s "
        "dev_flags=%(dev_flags)s"
    ) % account
