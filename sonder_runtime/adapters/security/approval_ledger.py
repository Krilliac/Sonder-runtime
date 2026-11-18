"""One-shot approvals for exactly one refused call, and the calls that asked.

The permission gate refuses the effect classes -- file changes, host
programs, destructive tools -- when nobody is present to answer a mode's
``ask``. This ledger is the fourth route out, next to an allow rule, a mode
that allows the class, and the console prompt: an operator approves *exactly
one call* (one tool, one argument digest) and the next unchanged call from
any surface runs once. Nothing here widens a rule, nothing survives one use
or its expiry. The ledger stores no argument values, with one exception: a
``run_code`` pending call carries a bounded, redacted copy of its code
(``detail``) so the operator can read what they approve. Otherwise the digest
and a bounded, content-free preview are what an operator sees and approves.

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

Two additions serve a chat turn that waits inside its request for an operator
to answer one specific call. ``open_for`` and ``denied_for`` are read-only
queries (they never spend, restore, revoke or write); ``deny`` records that an
operator refused a pending call. A pending call may also carry a ``detail``:
a bounded, redacted copy of the exact code a ``run_code`` call would run, so
the operator approves what they can read (the preview shows a bulk payload
only by its length). The digest never depends on any of this.

A denial is sticky: for ``DENIAL_TTL_SECONDS`` after ``deny`` the pending call
stays denied. A fresh refusal (``record_pending``) does not clear it, and
``issue`` refuses to approve it (``ApprovalDeniedError``) inside the same
transaction that reads the denial, so a concurrent approve and deny cannot both
succeed. Only ``issue(..., override_denial=True)`` -- used by the console
``/approve`` command alone -- clears it, and it does so in the issuing
transaction. After the TTL a denial lapses and a fresh refusal starts again.
"""
from __future__ import annotations

from sonder_runtime.adapters.persistence.owned_sqlite import connect as owned_sqlite_connect

import contextlib
import contextvars
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
MAX_DETAIL_CHARS = 4000
DENIAL_TTL_SECONDS = 900
PENDING_RETENTION_SECONDS = 7 * 86_400
APPROVAL_RETENTION_SECONDS = 30 * 86_400
DATABASE_NAME = "approvals.db"
DATABASE_ENV = "SONDER_APPROVALS_DB"

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS pending_calls (
        owner TEXT NOT NULL DEFAULT '',
        digest TEXT NOT NULL,
        tool TEXT NOT NULL,
        surface TEXT NOT NULL DEFAULT '',
        preview TEXT NOT NULL DEFAULT '',
        first_ts REAL NOT NULL,
        last_ts REAL NOT NULL,
        count INTEGER NOT NULL DEFAULT 1,
        detail TEXT NOT NULL DEFAULT '',
        denied_ts REAL,
        detail_chars INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (owner, digest)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS approvals (
        nonce TEXT PRIMARY KEY,
        owner TEXT NOT NULL DEFAULT '',
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
    "CREATE INDEX IF NOT EXISTS approvals_call ON approvals (owner, tool, digest)",
)

# Columns added after the first release: a database created by an older build
# lacks them, so ``_connect`` adds each one that is missing (nullable or
# defaulted, so existing rows stay valid).
_PENDING_COLUMNS = (
    ("detail", "TEXT NOT NULL DEFAULT ''"),
    ("denied_ts", "REAL"),
    ("detail_chars", "INTEGER NOT NULL DEFAULT 0"),
)

_OWNER_SCOPE: contextvars.ContextVar[str] = contextvars.ContextVar(
    "sonder_approval_ledger_owner", default=""
)
_INCLUDE_UNOWNED: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "sonder_approval_ledger_include_unowned", default=False
)


@contextlib.contextmanager
def owner_scope(owner: str = "", *, include_unowned: bool = False) -> Iterator[None]:
    """Bind the active principal owner key for approval ledger operations."""
    token = _OWNER_SCOPE.set(str(owner or "").strip())
    unowned_token = _INCLUDE_UNOWNED.set(bool(include_unowned))
    try:
        yield
    finally:
        _INCLUDE_UNOWNED.reset(unowned_token)
        _OWNER_SCOPE.reset(token)


def current_owner() -> str:
    return _OWNER_SCOPE.get()


# One definition of "spendable" shared by ``open_for`` and ``consume``, so the
# query and the spend can only disagree by a race: not consumed, not revoked,
# not expired, and the pending call has not been denied within the denial TTL.
# Parameters: owner, tool, digest, now, owner, now - DENIAL_TTL_SECONDS.
_SPENDABLE = (
    "owner=? AND tool=? AND digest=? AND consumed_ts IS NULL AND revoked_ts IS NULL "
    "AND expires_ts > ? "
    "AND NOT EXISTS (SELECT 1 FROM pending_calls p "
    "WHERE p.owner=? AND p.digest=approvals.digest AND p.denied_ts IS NOT NULL AND p.denied_ts > ?)"
)


class ApprovalLedgerError(ValueError):
    """A call id that resolves to nothing or to more than one call, or a bad TTL."""


class ApprovalDeniedError(ApprovalLedgerError):
    """``issue`` refused: an operator denied this call and the denial still stands."""

    denied = True


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
    detail: str = ""
    denied_ts: float | None = None
    detail_chars: int = 0
    owner: str = ""

    @property
    def call_id(self) -> str:
        return call_id(self.digest)

    @property
    def denied(self) -> bool:
        """Denied and still within ``DENIAL_TTL_SECONDS``."""
        return self.denied_ts is not None and self.denied_ts > time.time() - DENIAL_TTL_SECONDS

    @property
    def detail_truncated(self) -> bool:
        return self.detail_chars > MAX_DETAIL_CHARS


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

    def scope(self, owner: str = "", *, include_unowned: bool = False) -> contextlib.AbstractContextManager[None]:
        """Bind the principal owner scope for approval ledger calls."""
        return owner_scope(owner, include_unowned=include_unowned)

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
        for statement in _SCHEMA[:2]:
            conn.execute(statement)
        existing = {str(r["name"]) for r in conn.execute("PRAGMA table_info(pending_calls)")}
        for name, ddl in _PENDING_COLUMNS:
            if name in existing:
                continue
            try:
                conn.execute("ALTER TABLE pending_calls ADD COLUMN %s %s" % (name, ddl))
            except sqlite3.OperationalError as exc:
                # A concurrent connection added it first.
                if "duplicate column" not in str(exc).lower():
                    raise
        if "owner" not in existing:
            conn.execute("BEGIN IMMEDIATE")
            try:
                still = {str(r["name"]) for r in conn.execute("PRAGMA table_info(pending_calls)")}
                if "owner" not in still:
                    conn.executescript(
                        """
                        CREATE TABLE pending_calls_v2 (
                            owner TEXT NOT NULL DEFAULT '',
                            digest TEXT NOT NULL,
                            tool TEXT NOT NULL,
                            surface TEXT NOT NULL DEFAULT '',
                            preview TEXT NOT NULL DEFAULT '',
                            first_ts REAL NOT NULL,
                            last_ts REAL NOT NULL,
                            count INTEGER NOT NULL DEFAULT 1,
                            detail TEXT NOT NULL DEFAULT '',
                            denied_ts REAL,
                            detail_chars INTEGER NOT NULL DEFAULT 0,
                            PRIMARY KEY (owner, digest)
                        );
                        INSERT INTO pending_calls_v2
                            (owner, digest, tool, surface, preview, first_ts, last_ts, count, detail, denied_ts, detail_chars)
                        SELECT '', digest, tool, surface, preview, first_ts, last_ts, count, detail, denied_ts, detail_chars
                        FROM pending_calls;
                        DROP TABLE pending_calls;
                        ALTER TABLE pending_calls_v2 RENAME TO pending_calls;
                        """
                    )
                if conn.in_transaction:
                    conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        approval_cols = {str(r["name"]) for r in conn.execute("PRAGMA table_info(approvals)")}
        if "owner" not in approval_cols:
            try:
                conn.execute("ALTER TABLE approvals ADD COLUMN owner TEXT NOT NULL DEFAULT ''")
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise
        conn.execute(_SCHEMA[2])
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

    def _clean_detail(self, detail: str) -> tuple[str, int]:
        """Redact, then bound, the exact text an operator reads before approving.

        Returns the stored text and the length of the original (unredacted) text.
        """
        text = str(detail or "")
        original = len(text)
        if not text:
            return "", 0
        if self._redact is not None:
            try:
                text = str(self._redact(text))
            except Exception:
                return "", original
        if len(text) > MAX_DETAIL_CHARS:
            text = "%s\n...[truncated: first %d of %d chars shown]" % (
                text[:MAX_DETAIL_CHARS], MAX_DETAIL_CHARS, original)
        return text, original

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
                       preview: str = "", detail: str = "", owner: str | None = None) -> PendingCall:
        """Note one refused effect-class call so an operator can approve it.

        A denial of the same digest within ``DENIAL_TTL_SECONDS`` is kept, so a
        retry or a concurrent refusal can neither erase it before a waiting turn
        reads it nor reopen the call; once the TTL has passed it is cleared.
        """
        tool = str(tool or "").strip().lstrip("/")
        digest = str(digest or "").strip().lower()
        if not tool or len(digest) != 64:
            raise ApprovalLedgerError("a pending call needs a tool and a full digest")
        owner_key = current_owner() if owner is None else str(owner or "").strip()
        now = time.time()
        clean = self._clean_preview(preview)
        clean_detail, detail_chars = self._clean_detail(detail)
        with self._write() as conn:
            self._prune(conn, now)
            conn.execute(
                """
                INSERT INTO pending_calls (owner, digest, tool, surface, preview, first_ts, last_ts,
                                           count, detail, detail_chars)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(owner, digest) DO UPDATE SET
                    last_ts=excluded.last_ts,
                    surface=excluded.surface,
                    preview=CASE WHEN excluded.preview != '' THEN excluded.preview ELSE preview END,
                    detail=CASE WHEN excluded.detail != '' THEN excluded.detail ELSE detail END,
                    detail_chars=CASE WHEN excluded.detail != '' THEN excluded.detail_chars
                                      ELSE detail_chars END,
                    denied_ts=CASE WHEN denied_ts IS NOT NULL AND denied_ts > ?
                                   THEN denied_ts ELSE NULL END,
                    count=count+1
                """,
                (owner_key, digest, tool, str(surface or ""), clean, now, now, clean_detail,
                 detail_chars, now - DENIAL_TTL_SECONDS),
            )
            row = conn.execute(
                "SELECT * FROM pending_calls WHERE owner=? AND digest=?", (owner_key, digest)
            ).fetchone()
        return self._pending(row)

    def resolve_call(self, call_id_or_digest: str, *, owner: str | None = None,
                     include_unowned: bool | None = None) -> PendingCall:
        """The one pending call whose digest starts with ``call_id_or_digest``."""
        prefix = str(call_id_or_digest or "").strip().lower()
        if len(prefix) < MIN_CALL_ID_CHARS or any(c not in "0123456789abcdef" for c in prefix):
            raise ApprovalLedgerError(
                "a call id is at least %d hex characters (see /approvals)" % MIN_CALL_ID_CHARS
            )
        owner_key = current_owner() if owner is None else str(owner or "").strip()
        unowned = _INCLUDE_UNOWNED.get() if include_unowned is None else bool(include_unowned)
        with self._read() as conn:
            if unowned and owner_key:
                rows = conn.execute(
                    "SELECT * FROM pending_calls WHERE owner IN (?, '') AND digest LIKE ? ORDER BY last_ts DESC LIMIT 2",
                    (owner_key, prefix + "%"),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM pending_calls WHERE owner=? AND digest LIKE ? ORDER BY last_ts DESC LIMIT 2",
                    (owner_key, prefix + "%"),
                ).fetchall()
        if not rows:
            raise ApprovalLedgerError("no pending call starts with %r" % prefix)
        if len(rows) > 1:
            raise ApprovalLedgerError("call id %r is ambiguous; give more characters" % prefix)
        return self._pending(rows[0])

    def pending(self, limit: int = 20, *, owner: str | None = None,
                include_unowned: bool | None = None) -> list[PendingCall]:
        owner_key = current_owner() if owner is None else str(owner or "").strip()
        unowned = _INCLUDE_UNOWNED.get() if include_unowned is None else bool(include_unowned)
        with self._read() as conn:
            if unowned and owner_key:
                rows = conn.execute(
                    "SELECT * FROM pending_calls WHERE owner IN (?, '') ORDER BY last_ts DESC LIMIT ?",
                    (owner_key, max(1, min(int(limit), 200))),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM pending_calls WHERE owner=? ORDER BY last_ts DESC LIMIT ?",
                    (owner_key, max(1, min(int(limit), 200))),
                ).fetchall()
        return [self._pending(row) for row in rows]

    # -- approvals ---------------------------------------------------------

    def issue(self, tool: str, digest: str, *, approver: str, surface: str = "",
              ttl_seconds: int = DEFAULT_TTL_SECONDS, preview: str = "",
              override_denial: bool = False, owner: str | None = None) -> Approval:
        """Approve exactly one future call of ``tool`` with argument ``digest``.

        Raises ``ApprovalDeniedError`` while an operator's denial of this call
        stands (``DENIAL_TTL_SECONDS``); the check runs in the issuing
        transaction, so it serialises with ``deny``. ``override_denial=True``
        clears the denial and issues in that same transaction; only the
        console ``/approve`` command passes it.
        """
        tool = str(tool or "").strip().lstrip("/")
        digest = str(digest or "").strip().lower()
        approver = str(approver or "").strip()
        if not tool or len(digest) != 64:
            raise ApprovalLedgerError("an approval needs a tool and a full digest")
        if not approver:
            raise ApprovalLedgerError("an approval must name who approved it")
        owner_key = current_owner() if owner is None else str(owner or "").strip()
        unowned = _INCLUDE_UNOWNED.get() if owner is None else False
        ttl = clamp_ttl(ttl_seconds)
        now = time.time()
        nonce = "apv_" + secrets.token_hex(8)
        clean = self._clean_preview(preview)
        with self._write() as conn:
            self._prune(conn, now)
            if unowned and owner_key:
                match = conn.execute(
                    "SELECT owner FROM pending_calls WHERE owner IN (?, '') AND digest=? ORDER BY owner DESC LIMIT 1",
                    (owner_key, digest),
                ).fetchone()
                if match is not None:
                    owner_key = str(match["owner"])
            standing = conn.execute(
                "SELECT denied_ts FROM pending_calls WHERE owner=? AND digest=?",
                (owner_key, digest),
            ).fetchone()
            if (standing is not None and standing["denied_ts"] is not None
                    and float(standing["denied_ts"]) > now - DENIAL_TTL_SECONDS):
                if not override_denial:
                    raise ApprovalDeniedError(
                        "call %s was denied; it must be refused again after %d minutes, "
                        "or approved at the console, before it can be approved"
                        % (call_id(digest), DENIAL_TTL_SECONDS // 60)
                    )
                conn.execute(
                    "UPDATE pending_calls SET denied_ts=NULL WHERE owner=? AND digest=?",
                    (owner_key, digest),
                )
            if not clean:
                row = conn.execute(
                    "SELECT preview FROM pending_calls WHERE owner=? AND digest=?",
                    (owner_key, digest),
                ).fetchone()
                clean = str(row["preview"]) if row is not None else ""
            conn.execute(
                """
                INSERT INTO approvals (nonce, owner, tool, digest, approver, surface, preview,
                                       issued_ts, expires_ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (nonce, owner_key, tool, digest, approver, str(surface or ""), clean, now, now + ttl),
            )
            row = conn.execute("SELECT * FROM approvals WHERE nonce=?", (nonce,)).fetchone()
        return self._approval(row)

    def consume(self, tool: str, digest: str, *, surface: str = "",
                owner: str | None = None) -> Approval | None:
        """Spend one open approval for exactly this call, atomically, or None."""
        tool = str(tool or "").strip().lstrip("/")
        digest = str(digest or "").strip().lower()
        if not tool or len(digest) != 64:
            return None
        owner_key = current_owner() if owner is None else str(owner or "").strip()
        now = time.time()
        with self._write() as conn:
            row = conn.execute(
                "SELECT nonce FROM approvals WHERE " + _SPENDABLE
                + " ORDER BY issued_ts LIMIT 1",
                (owner_key, tool, digest, now, owner_key, now - DENIAL_TTL_SECONDS),
            ).fetchone()
            if row is None:
                return None
            cursor = conn.execute(
                """
                UPDATE approvals SET consumed_ts=?, consumed_surface=?
                WHERE nonce=? AND owner=? AND consumed_ts IS NULL AND revoked_ts IS NULL
                """,
                (now, str(surface or ""), row["nonce"], owner_key),
            )
            if cursor.rowcount != 1:
                return None
            # The request that asked for this call is answered.
            conn.execute(
                "DELETE FROM pending_calls WHERE owner=? AND digest=?",
                (owner_key, digest),
            )
            spent = conn.execute(
                "SELECT * FROM approvals WHERE nonce=?", (row["nonce"],)
            ).fetchone()
        return self._approval(spent)

    def open_for(self, tool: str, digest: str, *, owner: str | None = None) -> bool:
        """Whether an approval for exactly this call could be spent right now.

        Read-only: it never spends, restores, revokes or writes anything, so a
        waiting turn may poll it as often as it likes. It applies the same
        rules ``consume`` does (exact tool and digest, unspent, unrevoked,
        unexpired, call not denied). It is a hint, not an authority: an
        approval can be spent, revoked, denied or expire between this answer
        and the caller's next step, so ``consume`` (through the permission
        gate) stays the only thing that decides whether the call runs.
        """
        tool = str(tool or "").strip().lstrip("/")
        digest = str(digest or "").strip().lower()
        if not tool or len(digest) != 64 or not Path(self.path).exists():
            return False
        owner_key = current_owner() if owner is None else str(owner or "").strip()
        now = time.time()
        with self._read() as conn:
            row = conn.execute(
                "SELECT 1 FROM approvals WHERE " + _SPENDABLE + " LIMIT 1",
                (owner_key, tool, digest, now, owner_key, now - DENIAL_TTL_SECONDS),
            ).fetchone()
        return row is not None

    def deny(self, call_id_or_digest: str, *, owner: str | None = None,
             include_unowned: bool | None = None) -> PendingCall:
        """Record that an operator declined one pending call.

        The pending call stays listed, marked denied, so a waiting turn sees it
        through ``denied_for``. Any open approval for that call is revoked, and
        while the denial stands ``consume``, ``open_for`` and ``issue`` refuse
        the call. A denial authorises nothing and is scoped to one digest. It
        lasts ``DENIAL_TTL_SECONDS``; a refusal inside that window does not
        clear it.
        """
        owner_key = current_owner() if owner is None else str(owner or "").strip()
        pending = self.resolve_call(
            call_id_or_digest,
            owner=owner_key,
            include_unowned=_INCLUDE_UNOWNED.get() if include_unowned is None else bool(include_unowned),
        )
        target_owner = pending.owner if hasattr(pending, "owner") else owner_key
        now = time.time()
        with self._write() as conn:
            cursor = conn.execute(
                "UPDATE pending_calls SET denied_ts=? WHERE owner=? AND digest=?",
                (now, target_owner, pending.digest),
            )
            if cursor.rowcount != 1:
                raise ApprovalLedgerError("call %s is no longer pending" % pending.call_id)
            conn.execute(
                """
                UPDATE approvals SET revoked_ts=?
                WHERE owner=? AND tool=? AND digest=? AND consumed_ts IS NULL AND revoked_ts IS NULL
                      AND expires_ts > ?
                """,
                (now, target_owner, pending.tool, pending.digest, now),
            )
            row = conn.execute(
                "SELECT * FROM pending_calls WHERE owner=? AND digest=?",
                (target_owner, pending.digest),
            ).fetchone()
        return self._pending(row)

    def denied_for(self, tool: str, digest: str, *, owner: str | None = None) -> bool:
        """Read-only: whether an operator declined this exact pending call.

        True for ``DENIAL_TTL_SECONDS`` after the denial. Like ``open_for`` it
        neither creates nor migrates a missing database.
        """
        tool = str(tool or "").strip().lstrip("/")
        digest = str(digest or "").strip().lower()
        if not tool or len(digest) != 64 or not Path(self.path).exists():
            return False
        owner_key = current_owner() if owner is None else str(owner or "").strip()
        with self._read() as conn:
            row = conn.execute(
                "SELECT 1 FROM pending_calls WHERE owner=? AND tool=? AND digest=? "
                "AND denied_ts IS NOT NULL AND denied_ts > ?",
                (owner_key, tool, digest, time.time() - DENIAL_TTL_SECONDS),
            ).fetchone()
        return row is not None

    def restore(self, nonce: str, digest: str, *, owner: str | None = None) -> Approval | None:
        """Give back an approval spent on a call that did not take effect.

        Only a spent, unrevoked, unexpired approval for exactly ``digest``
        reopens, and it keeps its original expiry: restoring never extends an
        approval, and a withdrawn or lapsed one stays dead. The caller decides
        whether the call took effect (``permission_modes.restore_spent_approval``).
        """
        nonce = str(nonce or "").strip()
        digest = str(digest or "").strip().lower()
        owner_key = current_owner() if owner is None else str(owner or "").strip()
        now = time.time()
        with self._write() as conn:
            cursor = conn.execute(
                """
                UPDATE approvals SET consumed_ts=NULL, consumed_surface=''
                WHERE nonce=? AND owner=? AND digest=? AND consumed_ts IS NOT NULL
                      AND revoked_ts IS NULL AND expires_ts > ?
                """,
                (nonce, owner_key, digest, now),
            )
            if cursor.rowcount != 1:
                return None
            row = conn.execute("SELECT * FROM approvals WHERE nonce=?", (nonce,)).fetchone()
        return self._approval(row)

    def revoke(self, nonce: str, *, owner: str | None = None,
               include_unowned: bool | None = None) -> Approval | None:
        """Withdraw an open approval; returns it, or None if there is none open."""
        nonce = str(nonce or "").strip()
        owner_key = current_owner() if owner is None else str(owner or "").strip()
        unowned = _INCLUDE_UNOWNED.get() if include_unowned is None else bool(include_unowned)
        now = time.time()
        owner_sql = "owner IN (?, '')" if (unowned and owner_key) else "owner=?"
        with self._write() as conn:
            cursor = conn.execute(
                f"""
                UPDATE approvals SET revoked_ts=?
                WHERE nonce=? AND {owner_sql} AND consumed_ts IS NULL AND revoked_ts IS NULL AND expires_ts > ?
                """,
                (now, nonce, owner_key, now),
            )
            if cursor.rowcount != 1:
                return None
            row = conn.execute("SELECT * FROM approvals WHERE nonce=?", (nonce,)).fetchone()
        return self._approval(row)

    def get(self, nonce: str, *, owner: str | None = None) -> Approval | None:
        owner_key = current_owner() if owner is None else str(owner or "").strip()
        with self._read() as conn:
            row = conn.execute(
                "SELECT * FROM approvals WHERE nonce=? AND owner=?",
                (str(nonce or "").strip(), owner_key),
            ).fetchone()
        return self._approval(row) if row is not None else None

    def approvals(self, *, include_spent: bool = False, limit: int = 20,
                  owner: str | None = None, include_unowned: bool | None = None) -> list[Approval]:
        owner_key = current_owner() if owner is None else str(owner or "").strip()
        unowned = _INCLUDE_UNOWNED.get() if include_unowned is None else bool(include_unowned)
        now = time.time()
        with self._read() as conn:
            owner_sql = "owner IN (?, '')" if (unowned and owner_key) else "owner=?"
            if include_spent:
                rows = conn.execute(
                    f"SELECT * FROM approvals WHERE {owner_sql} ORDER BY issued_ts DESC LIMIT ?",
                    (owner_key, max(1, min(int(limit), 200))),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"""
                    SELECT * FROM approvals
                    WHERE {owner_sql} AND consumed_ts IS NULL AND revoked_ts IS NULL AND expires_ts > ?
                    ORDER BY issued_ts DESC LIMIT ?
                    """,
                    (owner_key, now, max(1, min(int(limit), 200))),
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
            detail=str(row["detail"] or ""),
            denied_ts=None if row["denied_ts"] is None else float(row["denied_ts"]),
            detail_chars=int(row["detail_chars"] or 0),
            owner=str(row["owner"] or "") if "owner" in row.keys() else "",
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
    "Approval", "ApprovalDeniedError", "ApprovalLedger", "ApprovalLedgerError",
    "DEFAULT_TTL_SECONDS", "DENIAL_TTL_SECONDS",
    "MAX_DETAIL_CHARS", "MAX_TTL_SECONDS", "MIN_TTL_SECONDS", "PendingCall", "call_id", "clamp_ttl",
    "current_owner", "default_ledger", "owner_scope",
]
