"""Local account, role, ban, and rate-limit helpers for hosted Trilobite."""
from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3
import time


SESSION_TTL_SECONDS = 60 * 60 * 24 * 14
DEFAULT_RATE_LIMIT = 120
RATE_WINDOW_SECONDS = 60
BOOTSTRAP_SECRET_MIN_LENGTH = 16


def _secret() -> str:
    return os.environ.get("TRILOBITE_AUTH_SECRET") or "trilobite-local-dev-secret"


def _bootstrap_secret() -> str:
    return os.environ.get("TRILOBITE_BOOTSTRAP_SECRET", "")


def _now() -> int:
    return int(time.time())


def _new_token() -> str:
    return os.urandom(24).hex()


def _hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or os.urandom(16).hex()
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        (password or "").encode("utf-8"),
        salt.encode("ascii"),
        120_000,
    ).hex()
    return salt, digest


def _hash_token(token: str) -> str:
    return hmac.new(
        _secret().encode("utf-8"),
        (token or "").encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS accounts (
            username TEXT PRIMARY KEY,
            password_salt TEXT,
            password_hash TEXT,
            role TEXT DEFAULT 'user',
            tier TEXT DEFAULT 'free',
            dev_flags TEXT DEFAULT '',
            banned INTEGER DEFAULT 0,
            created_ts INTEGER,
            last_login_ts INTEGER
        );
        CREATE TABLE IF NOT EXISTS account_sessions (
            token_hash TEXT PRIMARY KEY,
            username TEXT,
            created_ts INTEGER,
            expires_ts INTEGER,
            revoked INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS account_rate (
            username TEXT,
            window_start INTEGER,
            count INTEGER,
            PRIMARY KEY(username, window_start)
        );
        CREATE TABLE IF NOT EXISTS auth_bootstrap_state (
            singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
            consumed_ts INTEGER,
            consumed_by TEXT
        );
        """
    )
    conn.commit()


def account_count(conn: sqlite3.Connection) -> int:
    init(conn)
    return conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]


def register(
    conn: sqlite3.Connection,
    username: str,
    password: str,
    role: str = "user",
    *,
    trusted_local: bool = True,
    bootstrap_secret: str = "",
    allow_additional: bool = False,
    actor: dict | None = None,
) -> dict:
    """Create an account under an explicit registration policy.

    Direct callers are a trusted local path for backward compatibility. Hosted
    callers pass trusted_local=False; the first account then consumes the
    configured bootstrap secret exactly once. Later hosted registration needs
    both an admin actor and an explicit opt-in.
    """
    init(conn)
    username = (username or "").strip().lower()
    if not username or len(username) < 3:
        raise ValueError("username must be at least 3 characters")
    if not password or len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    role = role if role in ("user", "developer", "admin") else "user"
    salt, digest = _hash_password(password)
    conn.execute("BEGIN IMMEDIATE")
    try:
        first_account = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0
        if first_account:
            role = "admin"
            if not trusted_local:
                configured = _bootstrap_secret()
                state = conn.execute(
                    "SELECT consumed_ts FROM auth_bootstrap_state WHERE singleton=1"
                ).fetchone()
                valid = (
                    len(configured) >= BOOTSTRAP_SECRET_MIN_LENGTH
                    and hmac.compare_digest(
                        configured.encode("utf-8"),
                        (bootstrap_secret or "").encode("utf-8"),
                    )
                )
                if (state and state["consumed_ts"]) or not valid:
                    raise PermissionError("first-admin bootstrap is not authorized")
        else:
            if not trusted_local:
                ok, message = require(actor, "admin")
                if not allow_additional or not ok:
                    raise PermissionError(
                        message if allow_additional and message else "registration is disabled"
                    )
        conn.execute(
            "INSERT INTO accounts(username, password_salt, password_hash, role, created_ts) "
            "VALUES(?, ?, ?, ?, ?)",
            (username, salt, digest, role, _now()),
        )
        if first_account and not trusted_local:
            conn.execute(
                "INSERT INTO auth_bootstrap_state(singleton, consumed_ts, consumed_by) "
                "VALUES(1, ?, ?) ON CONFLICT(singleton) DO UPDATE SET "
                "consumed_ts=excluded.consumed_ts, consumed_by=excluded.consumed_by",
                (_now(), username),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return public_account(conn, username)


def public_account(conn: sqlite3.Connection, username: str) -> dict:
    init(conn)
    row = conn.execute(
        "SELECT username, role, tier, dev_flags, banned, created_ts, last_login_ts "
        "FROM accounts WHERE username=?",
        ((username or "").strip().lower(),),
    ).fetchone()
    if not row:
        raise ValueError("unknown account")
    return {
        "username": row["username"],
        "role": row["role"],
        "tier": row["tier"],
        "dev_flags": row["dev_flags"] or "",
        "banned": bool(row["banned"]),
        "created_ts": row["created_ts"],
        "last_login_ts": row["last_login_ts"],
    }


def login(conn: sqlite3.Connection, username: str, password: str) -> tuple[str, dict]:
    init(conn)
    username = (username or "").strip().lower()
    row = conn.execute("SELECT * FROM accounts WHERE username=?", (username,)).fetchone()
    if not row:
        raise ValueError("invalid username or password")
    if row["banned"]:
        raise PermissionError("account is banned")
    _, digest = _hash_password(password, row["password_salt"])
    if not hmac.compare_digest(digest, row["password_hash"]):
        raise ValueError("invalid username or password")
    token = _new_token()
    conn.execute(
        "INSERT INTO account_sessions(token_hash, username, created_ts, expires_ts) "
        "VALUES(?, ?, ?, ?)",
        (_hash_token(token), username, _now(), _now() + SESSION_TTL_SECONDS),
    )
    conn.execute("UPDATE accounts SET last_login_ts=? WHERE username=?", (_now(), username))
    conn.commit()
    return token, public_account(conn, username)


def authenticate(conn: sqlite3.Connection, token: str) -> dict | None:
    init(conn)
    if not token:
        return None
    row = conn.execute(
        "SELECT username, expires_ts, revoked FROM account_sessions WHERE token_hash=?",
        (_hash_token(token),),
    ).fetchone()
    if not row or row["revoked"] or int(row["expires_ts"] or 0) < _now():
        return None
    account = public_account(conn, row["username"])
    if account["banned"]:
        return None
    return account


def require(account: dict | None, role: str = "user") -> tuple[bool, str]:
    if not account:
        return False, "login required"
    ranks = {"user": 0, "developer": 1, "admin": 2}
    if ranks.get(account.get("role", "user"), 0) < ranks.get(role, 0):
        return False, "%s role required" % role
    if account.get("banned"):
        return False, "account is banned"
    return True, ""


def set_account(conn: sqlite3.Connection, username: str, **changes) -> dict:
    init(conn)
    username = (username or "").strip().lower()
    allowed = {"role", "tier", "dev_flags", "banned"}
    assignments = []
    values = []
    for key, value in changes.items():
        if key not in allowed:
            continue
        if key == "banned":
            value = 1 if value else 0
        assignments.append("%s=?" % key)
        values.append(value)
    if assignments:
        values.append(username)
        conn.execute("UPDATE accounts SET %s WHERE username=?" % ", ".join(assignments), values)
        conn.commit()
    return public_account(conn, username)


def list_accounts(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    init(conn)
    rows = conn.execute(
        "SELECT username FROM accounts ORDER BY created_ts DESC LIMIT ?",
        (max(1, min(200, int(limit or 50))),),
    ).fetchall()
    return [public_account(conn, row["username"]) for row in rows]


def rate_limit(conn: sqlite3.Connection, account: dict | None, cost: int = 1) -> tuple[bool, str]:
    init(conn)
    if not account:
        return True, ""
    limit = DEFAULT_RATE_LIMIT
    if account.get("tier") == "pro":
        limit = 600
    if account.get("tier") == "enterprise":
        limit = 3000
    if "unlimited" in (account.get("dev_flags") or ""):
        return True, ""
    username = account["username"]
    window = (_now() // RATE_WINDOW_SECONDS) * RATE_WINDOW_SECONDS
    row = conn.execute(
        "SELECT count FROM account_rate WHERE username=? AND window_start=?",
        (username, window),
    ).fetchone()
    count = int(row["count"] if row else 0) + max(1, int(cost or 1))
    if row:
        conn.execute(
            "UPDATE account_rate SET count=? WHERE username=? AND window_start=?",
            (count, username, window),
        )
    else:
        conn.execute(
            "INSERT INTO account_rate(username, window_start, count) VALUES(?, ?, ?)",
            (username, window, count),
        )
    conn.commit()
    if count > limit:
        return False, "rate limit exceeded for tier %s (%d/min)" % (account.get("tier"), limit)
    return True, ""

