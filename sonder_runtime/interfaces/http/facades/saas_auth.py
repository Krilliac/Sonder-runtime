"""HTTP facade helpers for assigned API keys, mode-principal isolation, and SaaS metering.

Kept out of ``serve.py`` so HTTP auth/metering glue stays small, pure-injected,
and within the ``serve.py`` line-count ratchet.
"""
from __future__ import annotations

from typing import Any, Callable


def resolve_account_token(token: str, runtime: Any, account_auth: Any) -> dict[str, Any] | None:
    """Resolve a bearer token to an account dict (assigned ``sk-`` key or signed-in session)."""
    if not token:
        return None
    if not token.startswith(account_auth.API_KEY_PREFIX):
        resolved = runtime._admin_account_from_token(token)
        return resolved if isinstance(resolved, dict) else None
    conn = runtime._open_db()
    try:
        resolved = account_auth.api_key_account(conn, token)
        return resolved if isinstance(resolved, dict) else None
    finally:
        conn.close()


def mode_principal(context: Any, state_principal_of: Callable[[Any], str]) -> str:
    """Permission-mode scope: an assigned API key never shares its account session's mode/bypass."""
    principal = state_principal_of(context)
    account = (context or {}).get("account") or {}
    if isinstance(account, dict) and account.get("via_api_key"):
        return "api-key-of:" + principal
    return principal


def rate_limit_and_meter(conn: Any, account: dict[str, Any] | None, account_auth: Any) -> tuple[bool, str]:
    """Enforce per-minute tier rate limit, then monthly SaaS token metering when enabled."""
    ok, msg = account_auth.rate_limit(conn, account)
    if ok:
        ok, msg = account_auth.meter_usage(conn, account, account_auth.CHAT_REQUEST_TOKEN_ESTIMATE)
    return bool(ok), str(msg)


__all__ = ["mode_principal", "rate_limit_and_meter", "resolve_account_token"]
