"""One-shot approvals for exactly one refused call, and the calls that asked.

The permission gate refuses the effect classes -- file changes, host
programs, destructive tools -- when nobody is present to answer a mode's
``ask``. This ledger is the fourth route out, next to an allow rule, a mode
that allows the class, and the console prompt: an operator approves *exactly
one call* (one tool, one argument digest) and the next unchanged call from
any surface runs once. Nothing here widens a rule, nothing survives one use
or its expiry, and the ledger never stores arguments: the digest and a
bounded, content-free preview are what an operator sees and approves.

Two tables in one SQLite file (``approvals.db`` under the Sonder home;
``SONDER_APPROVALS_DB`` relocates it):

``pending_calls``  every unattended refusal of an effect-class call that
                   carried arguments, keyed by digest so a retried call counts
                   up instead of piling up. ``/approvals`` lists these and
                   ``/approve <call id>`` resolves one by the digest prefix.
``approvals``      one row per issued approval: nonce, tool, digest, who
                   approved it and from where, when it expires, and when (and
                   where) it was spent or revoked. Spending is one conditional
                   ``UPDATE`` inside an immediate transaction, so two identical
                   calls racing for one approval cannot both run.

The digest is ``permission_modes.call_digest``: the tool name plus the
canonical JSON of its arguments with the credential knobs removed, so the
approved call and the retried call hash the same whatever token or approval
string the surface added. The preview is ``permission_modes.argument_preview``
-- keys and short values, bulk payloads shown only by length -- passed
through the platform redactor before it is written.
"""
from __future__ import annotations

from sonder_runtime.adapters.persistence.owned_sqlite import connect as owned_sqlite_connect

import contextlib
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from ...platform import paths as runtime_paths

DEFAULT_TTL_SECONDS = 900
MIN_TTL_SECONDS = 60
MAX_TTL_SECONDS = 86_400
CALL_ID_CHARS = 16
MIN_CALL_ID_CHARS = 8
MAX_PREVIEW_CHARS = 200
PENDING_RETENTION_SECONDS = 7 * 86_400
APPROVAL_RETENTION_SECONDS = 30 * 86_400
DATABASE_NAME = "approvals.db"
DATABASE_ENV = "SONDER_APPROVALS_DB"

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS pending_calls (
        digest TEXT PRIMARY KEY,
        tool TEXT NOT NULL,
        surface TEXT NOT NULL DEFAULT '',
        preview TEXT NOT NULL DEFAULT '',
        first_ts REAL NOT NULL,
        last_ts REAL NOT NULL,
        count INTEGER NOT NULL DEFAULT 1
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS approvals (
        nonce TEXT PRIMARY KEY,
        tool TEXT NOT NULL,
        digest TEXT NOT NULL,
        approver TEXT NOT NULL,
        surface TEXT NOT NULL DEFAULT '',
        preview TEXT NOT NULL DEFAULT '',
        issued_ts REAL NOT NULL,
        expires_ts REAL NOT NULL,
        consumed_ts REAL,
        consumed_surface TEXT NOT NULL DEFAULT '',
        revoked_ts REAL
    )
    """,
    "CREATE INDEX IF NOT EXISTS approvals_call ON approvals (tool, digest)",
)


class ApprovalLedgerError(ValueError):
    """A call id that resolves to nothing or to more than one call, or a bad TTL."""


def call_id(digest: str) -> str:
    """The short form an operator types: the first 16 hex characters."""
    return str(digest or "")[:CALL_ID_CHARS]


def _stamp(ts: float | None) -> str:
    if not ts:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))


@dataclass(frozen=True)
class PendingCall:
    digest: str
    tool: str
    surface: str
    preview: str
    first_ts: float
    last_ts: float
    count: int

    @property
    def call_id(self) -> str:
        return call_id(self.digest)


@dataclass(frozen=True)
class Approval:
    nonce: str
    tool: str
    digest: str
    approver: str
    surface: str
    preview: str
    issued_ts: float
    expires_ts: float
    consumed_ts: float | None = None
    consumed_surface: str = ""
    revoked_ts: float | None = None

    @property
    def call_id(self) -> str:
        return call_id(self.digest)

    @property
    def spent(self) -> bool:
        return self.consumed_ts is not None

    @property
    def revoked(self) -> bool:
        return self.revoked_ts is not None

    def open(self, now: float | None = None) -> bool:
        """Still spendable: not consumed, not revoked, not expired."""
        now = time.time() if now is None else now
        return not self.spent and not self.revoked and self.expires_ts > now

    def state(self, now: float | None = None) -> str:
        if self.revoked:
            return "revoked"
        if self.spent:
            return "spent"
        if not self.open(now):
            return "expired"
        return "open"


def clamp_ttl(ttl_seconds) -> int:
    try:
        ttl = int(ttl_seconds)
    except (TypeError, ValueError):
        raise ApprovalLedgerError("ttl_seconds must be a whole number of seconds") from None
    if ttl < MIN_TTL_SECONDS or ttl > MAX_TTL_SECONDS:
        raise ApprovalLedgerError(
            "ttl_seconds must be between %d and %d" % (MIN_TTL_SECONDS, MAX_TTL_SECONDS)
        )
    return ttl


class ApprovalLedger:
    """The durable ledger; every method opens, uses and closes its own connection.

    ``path`` may be left unset, in which case it is resolved on every call from
    the configured Sonder home, so a process that reconfigures its home (the
    hermetic test home, ``SONDER_HOME``) never writes to a stale location.
    ``redact`` is applied to previews before they are stored.
    """

    def __init__(self, path: str | Path | None = None, *,
                 redact: Callable[[str], str] | None = None) -> None:
        self._path = str(path) if path else ""
        self._redact = redact

    @property
    def path(self) -> str:
        if self._path:
            return self._path
        return runtime_paths.state_path(DATABASE_NAME, DATABASE_ENV)

    def pinned(self):
        """Resolve configuration once for one multi-operation host decision.

        Preserve the adapter's preview redactor and injected adapter behavior,
        while preventing a concurrent home/config change from splitting a
        decision and its confirmation between databases.
        """
        import copy

        pinned = copy.copy(self)
        pinned._path = str(Path(self.path).resolve())
        return pinned

    # -- connections -------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        path = self.path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = owned_sqlite_connect(path, timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        for statement in _SCHEMA:
            conn.execute(statement)
        return conn

    @contextlib.contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()

    @contextlib.contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    def _clean_preview(self, preview: str) -> str:
        text = str(preview or "")
        if self._redact is not None:
            try:
                text = str(self._redact(text))
            except Exception:
                # A redactor that fails must not write the raw text; an
                # unpreviewed request is still approvable by its call id.
                text = ""
        return text[:MAX_PREVIEW_CHARS]

    @staticmethod
    def _prune(conn: sqlite3.Connection, now: float) -> None:
        conn.execute(
            "DELETE FROM pending_calls WHERE last_ts < ?",
            (now - PENDING_RETENTION_SECONDS,),
        )
        conn.execute(
            "DELETE FROM approvals WHERE expires_ts < ? AND "
            "(consumed_ts IS NOT NULL OR revoked_ts IS NOT NULL OR expires_ts < ?)",
            (now - APPROVAL_RETENTION_SECONDS, now - APPROVAL_RETENTION_SECONDS),
        )

    # -- pending calls -----------------------------------------------------

    def record_pending(self, tool: str, digest: str, *, surface: str = "",
                       preview: str = "") -> PendingCall:
        """Note one refused effect-class call so an operator can approve it."""
        tool = str(tool or "").strip().lstrip("/")
        digest = str(digest or "").strip().lower()
        if not tool or len(digest) != 64:
            raise ApprovalLedgerError("a pending call needs a tool and a full digest")
        now = time.time()
        clean = self._clean_preview(preview)
        with self._write() as conn:
            self._prune(conn, now)
            conn.execute(
                """
                INSERT INTO pending_calls (digest, tool, surface, preview, first_ts, last_ts, count)
                VALUES (?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(digest) DO UPDATE SET
                    last_ts=excluded.last_ts,
                    surface=excluded.surface,
                    preview=CASE WHEN excluded.preview != '' THEN excluded.preview ELSE preview END,
                    count=count+1
                """,
                (digest, tool, str(surface or ""), clean, now, now),
            )
            row = conn.execute(
                "SELECT * FROM pending_calls WHERE digest=?", (digest,)
            ).fetchone()
        return self._pending(row)

    def resolve_call(self, call_id_or_digest: str) -> PendingCall:
        """The one pending call whose digest starts with ``call_id_or_digest``."""
        prefix = str(call_id_or_digest or "").strip().lower()
        if len(prefix) < MIN_CALL_ID_CHARS or any(c not in "0123456789abcdef" for c in prefix):
            raise ApprovalLedgerError(
                "a call id is at least %d hex characters (see /approvals)" % MIN_CALL_ID_CHARS
            )
        with self._read() as conn:
            rows = conn.execute(
                "SELECT * FROM pending_calls WHERE digest LIKE ? ORDER BY last_ts DESC LIMIT 2",
                (prefix + "%",),
            ).fetchall()
        if not rows:
            raise ApprovalLedgerError("no pending call starts with %r" % prefix)
        if len(rows) > 1:
            raise ApprovalLedgerError("call id %r is ambiguous; give more characters" % prefix)
        return self._pending(rows[0])

    def pending(self, limit: int = 20) -> list[PendingCall]:
        with self._read() as conn:
            rows = conn.execute(
                "SELECT * FROM pending_calls ORDER BY last_ts DESC LIMIT ?",
                (max(1, min(int(limit), 200)),),
            ).fetchall()
        return [self._pending(row) for row in rows]

    # -- approvals ---------------------------------------------------------

    def issue(self, tool: str, digest: str, *, approver: str, surface: str = "",
              ttl_seconds: int = DEFAULT_TTL_SECONDS, preview: str = "") -> Approval:
        """Approve exactly one future call of ``tool`` with argument ``digest``."""
        tool = str(tool or "").strip().lstrip("/")
        digest = str(digest or "").strip().lower()
        approver = str(approver or "").strip()
        if not tool or len(digest) != 64:
            raise ApprovalLedgerError("an approval needs a tool and a full digest")
        if not approver:
            raise ApprovalLedgerError("an approval must name who approved it")
        ttl = clamp_ttl(ttl_seconds)
        now = time.time()
        nonce = "apv_" + secrets.token_hex(8)
        clean = self._clean_preview(preview)
        with self._write() as conn:
            self._prune(conn, now)
            if not clean:
                row = conn.execute(
                    "SELECT preview FROM pending_calls WHERE digest=?", (digest,)
                ).fetchone()
                clean = str(row["preview"]) if row is not None else ""
            conn.execute(
                """
                INSERT INTO approvals (nonce, tool, digest, approver, surface, preview,
                                       issued_ts, expires_ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (nonce, tool, digest, approver, str(surface or ""), clean, now, now + ttl),
            )
            row = conn.execute("SELECT * FROM approvals WHERE nonce=?", (nonce,)).fetchone()
        return self._approval(row)

    def consume(self, tool: str, digest: str, *, surface: str = "") -> Approval | None:
        """Spend one open approval for exactly this call, atomically, or None."""
        tool = str(tool or "").strip().lstrip("/")
        digest = str(digest or "").strip().lower()
        if not tool or len(digest) != 64:
            return None
        now = time.time()
        with self._write() as conn:
            row = conn.execute(
                """
                SELECT nonce FROM approvals
                WHERE tool=? AND digest=? AND consumed_ts IS NULL AND revoked_ts IS NULL
                      AND expires_ts > ?
                ORDER BY issued_ts LIMIT 1
                """,
                (tool, digest, now),
            ).fetchone()
            if row is None:
                return None
            cursor = conn.execute(
                """
                UPDATE approvals SET consumed_ts=?, consumed_surface=?
                WHERE nonce=? AND consumed_ts IS NULL AND revoked_ts IS NULL
                """,
                (now, str(surface or ""), row["nonce"]),
            )
            if cursor.rowcount != 1:
                return None
            # The request that asked for this call is answered.
            conn.execute("DELETE FROM pending_calls WHERE digest=?", (digest,))
            spent = conn.execute(
                "SELECT * FROM approvals WHERE nonce=?", (row["nonce"],)
            ).fetchone()
        return self._approval(spent)

    def revoke(self, nonce: str) -> Approval | None:
        """Withdraw an open approval; returns it, or None if there is none open."""
        nonce = str(nonce or "").strip()
        now = time.time()
        with self._write() as conn:
            cursor = conn.execute(
                """
                UPDATE approvals SET revoked_ts=?
                WHERE nonce=? AND consumed_ts IS NULL AND revoked_ts IS NULL AND expires_ts > ?
                """,
                (now, nonce, now),
            )
            if cursor.rowcount != 1:
                return None
            row = conn.execute("SELECT * FROM approvals WHERE nonce=?", (nonce,)).fetchone()
        return self._approval(row)

    def get(self, nonce: str) -> Approval | None:
        with self._read() as conn:
            row = conn.execute(
                "SELECT * FROM approvals WHERE nonce=?", (str(nonce or "").strip(),)
            ).fetchone()
        return self._approval(row) if row is not None else None

    def approvals(self, *, include_spent: bool = False, limit: int = 20) -> list[Approval]:
        now = time.time()
        with self._read() as conn:
            if include_spent:
                rows = conn.execute(
                    "SELECT * FROM approvals ORDER BY issued_ts DESC LIMIT ?",
                    (max(1, min(int(limit), 200)),),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM approvals
                    WHERE consumed_ts IS NULL AND revoked_ts IS NULL AND expires_ts > ?
                    ORDER BY issued_ts DESC LIMIT ?
                    """,
                    (now, max(1, min(int(limit), 200))),
                ).fetchall()
        return [self._approval(row) for row in rows]

    # -- presentation ------------------------------------------------------

    def format_status(self, limit: int = 20) -> str:
        """The operator's view: what asked, what is approved, what was spent."""
        now = time.time()
        pending = self.pending(limit)
        approvals = self.approvals(include_spent=True, limit=limit)
        lines = ["one-shot approvals"]
        lines.append("  pending calls (refused unattended; approve one with /approve <call id>):")
        if not pending:
            lines.append("    (none)")
        for item in pending:
            lines.append("    %s  %s  x%d  via %s  last %s  %s" % (
                item.call_id, item.tool, item.count, item.surface or "-",
                _stamp(item.last_ts), item.preview or "",
            ))
        lines.append("  approvals:")
        if not approvals:
            lines.append("    (none)")
        for item in approvals:
            state = item.state(now)
            tail = {
                "open": "expires %s" % _stamp(item.expires_ts),
                "spent": "spent %s via %s" % (_stamp(item.consumed_ts), item.consumed_surface or "-"),
                "revoked": "revoked %s" % _stamp(item.revoked_ts),
                "expired": "expired %s" % _stamp(item.expires_ts),
            }[state]
            lines.append("    %s  %s  call %s  %s  by %s  %s" % (
                item.nonce, item.tool, item.call_id, state, item.approver, tail,
            ))
        return "\n".join(lines)

    # -- rows --------------------------------------------------------------

    @staticmethod
    def _pending(row) -> PendingCall:
        return PendingCall(
            digest=str(row["digest"]), tool=str(row["tool"]), surface=str(row["surface"]),
            preview=str(row["preview"]), first_ts=float(row["first_ts"]),
            last_ts=float(row["last_ts"]), count=int(row["count"]),
        )

    @staticmethod
    def _approval(row) -> Approval:
        return Approval(
            nonce=str(row["nonce"]), tool=str(row["tool"]), digest=str(row["digest"]),
            approver=str(row["approver"]), surface=str(row["surface"]),
            preview=str(row["preview"]), issued_ts=float(row["issued_ts"]),
            expires_ts=float(row["expires_ts"]),
            consumed_ts=None if row["consumed_ts"] is None else float(row["consumed_ts"]),
            consumed_surface=str(row["consumed_surface"] or ""),
            revoked_ts=None if row["revoked_ts"] is None else float(row["revoked_ts"]),
        )


def default_ledger() -> ApprovalLedger:
    """The production ledger: the configured home, previews redacted."""
    from ...platform.logging import Redactor

    return ApprovalLedger(redact=Redactor().redact)


__all__ = [
    "Approval", "ApprovalLedger", "ApprovalLedgerError", "DEFAULT_TTL_SECONDS",
    "MAX_TTL_SECONDS", "MIN_TTL_SECONDS", "PendingCall", "call_id", "clamp_ttl",
    "default_ledger",
]
