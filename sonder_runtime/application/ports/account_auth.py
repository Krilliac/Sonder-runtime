"""Port for hosted account authentication and role checks."""
from __future__ import annotations

from typing import Any, Protocol


class AccountAuth(Protocol):
    def register(self, connection: Any, username: str, password: str) -> dict: ...
    def login(self, connection: Any, username: str, password: str) -> tuple[str, dict]: ...
    def require(self, account: dict | None, role: str = "user") -> tuple[bool, str]: ...
    def rate_limit(self, connection: Any, account: dict | None, cost: int = 1) -> tuple[bool, str]: ...


__all__ = ["AccountAuth"]
