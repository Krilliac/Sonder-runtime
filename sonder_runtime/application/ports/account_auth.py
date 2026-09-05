"""Port for hosted account authentication and role checks."""
from __future__ import annotations

from typing import Any, Protocol
from dataclasses import dataclass, field


@dataclass(frozen=True)
class AccountSessionIdentity:
    """Private live lookup result, never execution or enrollment permission.

    The reference is an internal exact-login lookup key. Keep it out of HTTP
    payloads, logs, transcripts and model context. Re-read it for every admission;
    a retained instance does not carry a lease or a current role guarantee.
    """

    reference: str = field(repr=False)
    username: str
    role: str
    expires_at: int


class AccountAuth(Protocol):
    def register(self, connection: Any, username: str, password: str) -> dict: ...
    def login(self, connection: Any, username: str, password: str) -> tuple[str, dict]: ...
    def reauthenticate(self, connection: Any, token: str, password: str) -> dict: ...
    def revoke_session(self, connection: Any, token: str) -> None: ...
    def authenticate_session(self, connection: Any, token: str) -> AccountSessionIdentity | None: ...
    def read_session_reference(self, connection: Any, reference: str) -> AccountSessionIdentity | None: ...
    def require(self, account: dict | None, role: str = "user") -> tuple[bool, str]: ...
    def rate_limit(self, connection: Any, account: dict | None, cost: int = 1) -> tuple[bool, str]: ...


__all__ = ["AccountAuth", "AccountSessionIdentity"]
