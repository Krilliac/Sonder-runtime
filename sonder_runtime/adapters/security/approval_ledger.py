"""One-shot approvals for exactly one refused call, and the calls that asked.

The permission gate refuses the effect classes -- file changes, host
programs, destructive tools -- when nobody is present to answer a mode's
``ask``. This ledger is the fourth route out, next to an allow rule, a mode
that allows the class, and the console prompt: an operator approves *exactly
one call* (one tool, one argument digest) and the next unchanged call from
any surface runs once -- the next one made by the owner whose refused call it
answers: an account's turns record and spend under that account's owner key,
the operator's own surfaces under the unowned key ''. Only an explicit
``operator_scope`` (bound by the console and the MCP entry after an
admin-level check, and by the HTTP routes for an administrator) sees and
approves every owner's calls; with no scope bound a caller sees exactly the
unowned ones; an account sees only its own; a developer its own plus the
unowned ones. Approve and deny bind to the exact pending row (its owner and
digest): two owners with an identical call get one reference each,
``<call id>@<owner tag>``, and the owner is never guessed. Nothing here widens
a rule, nothing survives one use or its expiry, and the ledger never stores
bulk payloads with one exception: a
``run_code`` pending call carries a bounded, redacted copy of its code
(``detail``) so the operator can read what they approve. Otherwise the digest
and a bounded, redacted preview of the rest are what an operator sees and
approves.

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
string the surface added. The preview is built from
``permission_modes.preview_fields`` -- every key and non-bulk value, bulk
payloads shown only by length -- by ``render_preview``: each ``key=value``
field is passed through the platform redactor as one unit, so key-context
patterns (``password=...``) still fire, yet one field's redaction cannot
swallow the arguments after it; then it is quoted. Control and bidi characters
are escaped. When redaction or truncation changed what is stored, the preview
starts with ``[approval covers text not shown: ...]`` and ``/approvals`` shows
it so; such a call is not approvable by its call id (``preview_complete``),
only by giving its full arguments.

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
import json
import re
import secrets
import sqlite3
import time
import unicodedata
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Iterator, Sequence, Union

from ...platform import paths as runtime_paths

DEFAULT_TTL_SECONDS = 900
MIN_TTL_SECONDS = 60
MAX_TTL_SECONDS = 86_400
CALL_ID_CHARS = 16
MIN_CALL_ID_CHARS = 8
# Large enough for a full command line: an operator approving by call id must
# be able to read what the call binds (``permission_modes.PREVIEW_CHARS``).
MAX_PREVIEW_CHARS = 2000
MAX_DETAIL_CHARS = 4000
DENIAL_TTL_SECONDS = 900
PREVIEW_TRUNCATED = " ...[preview truncated]"
# Leads a stored preview that does not show everything the approval covers
# (the redactor rewrote part of it, it was cut, or the redactor failed). The
# ledger adds it; text that merely starts the same way only makes its own call
# harder to approve.
PREVIEW_INCOMPLETE = "[approval covers text not shown: %s] "
_INCOMPLETE_HEAD = PREVIEW_INCOMPLETE.split("%s", 1)[0]
# A key or string value shown bare: one run of printable ASCII with no space,
# no double quote and no ``=``, so it can neither carry a terminal control or
# bidi character nor imitate a further ``key=value`` pair. Anything else is
# shown JSON-quoted with ``ensure_ascii``.
_BARE_PREVIEW_TOKEN = re.compile(r"[!#-<>-~]+\Z")

# ``(key, value, literal)``: ``value`` is the display text before redaction;
# ``literal`` marks text already in a quote-safe form (JSON, a length) that is
# shown unquoted.
PreviewField = tuple[str, str, bool]
Preview = Union[str, Sequence[PreviewField]]
UNOWNED_TAG = "-"
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

# Who is asking. ``None`` means no owner scope was ever bound on this context;
# it reads, writes and approves exactly the unowned ('') rows, the same as
# ``owner_scope("")``. It never grants the all-owners view: that takes an
# explicit ``operator_scope``, so a path that loses its context (a thread
# started without ``copy_context``) fails closed. An account turn always binds
# its owner key (``permission_modes.mode_scope`` does it for every
# ``account:`` principal).
_OWNER_SCOPE: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "sonder_approval_ledger_owner", default=None
)
_INCLUDE_UNOWNED: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "sonder_approval_ledger_include_unowned", default=False
)
_ALL_OWNERS: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "sonder_approval_ledger_all_owners", default=False
)


@contextlib.contextmanager
def owner_scope(owner: str = "", *, include_unowned: bool = False,
                all_owners: bool = False) -> Iterator[None]:
    """Bind the active principal owner key for approval ledger operations.

    ``include_unowned`` adds the unowned ('') rows to the owner's view (a
    developer). ``all_owners`` is the operator view (an administrator, or the
    single-owner local-open / deployment-key listener): it reads, resolves,
    approves, denies and revokes every owner's calls. Writes made on the
    caller's own behalf (``record_pending``, ``consume``, ``restore``) and the
    waiting-turn queries (``open_for``, ``denied_for``, ``get``) always use
    exactly the bound owner key.
    """
    token = _OWNER_SCOPE.set(str(owner or "").strip())
    unowned_token = _INCLUDE_UNOWNED.set(bool(include_unowned))
    all_token = _ALL_OWNERS.set(bool(all_owners))
    try:
        yield
    finally:
        _ALL_OWNERS.reset(all_token)
        _INCLUDE_UNOWNED.reset(unowned_token)
        _OWNER_SCOPE.reset(token)


def operator_scope() -> contextlib.AbstractContextManager[None]:
    """The operator view: every owner's pending calls and approvals.

    Bind it only after an admin-level check (the attended console, an
    administrator's token or session, the deployment key).
    """
    return owner_scope("", all_owners=True)


def current_owner() -> str:
    return _OWNER_SCOPE.get() or ""


def operator_view() -> bool:
    """True when this context sees every owner's calls.

    Only inside an explicit ``operator_scope`` / ``owner_scope(all_owners=True)``.
    An unbound context is not the operator: it sees the unowned rows only.
    """
    return bool(_ALL_OWNERS.get())


def owner_tag(owner: str) -> str:
    """The owner part of a call reference: the whole owner key, so two owners
    never share one (a prefix of it can collide), and ``-`` for the unowned key.
    ``resolve_call`` still accepts any typed prefix of it."""
    owner = str(owner or "")
    return owner if owner else UNOWNED_TAG


def call_ref(digest: str, owner: str = "") -> str:
    """What an operator types to name exactly one pending row: the call id
    qualified by its owner tag, ``<call id>@<owner tag>``; the unowned row is
    ``<call id>@-``. The bare call id stays accepted while only one owner has
    that call pending, but it is never shown, since a second owner's identical
    call would make it ambiguous."""
    return "%s@%s" % (call_id(digest), owner_tag(owner))


def _split_ref(text: str) -> tuple[str, str | None]:
    """``(lowercased digest prefix, owner tag or None)`` from a call reference."""
    head, sep, tag = str(text or "").strip().partition("@")
    return head.strip().lower(), (tag.strip() if sep else None)


_ALL = "all"
_WITH_UNOWNED = "with_unowned"
_EXACT = "exact"


def _read_scope(owner: str | None, include_unowned: bool | None) -> tuple[str, str]:
    """``(kind, owner_key)`` for an operator-facing read or resolve.

    An explicit ``owner`` argument is always exact (plus unowned rows when
    ``include_unowned`` is true); only the bound context can grant the
    operator view.
    """
    if owner is None:
        owner_key = current_owner()
        if operator_view():
            return _ALL, owner_key
        unowned = _INCLUDE_UNOWNED.get() if include_unowned is None else bool(include_unowned)
    else:
        owner_key = str(owner or "").strip()
        unowned = bool(include_unowned)
    if unowned and owner_key:
        return _WITH_UNOWNED, owner_key
    return _EXACT, owner_key


def _owner_sql(kind: str, owner_key: str) -> tuple[str, tuple[str, ...]]:
    if kind == _ALL:
        return "1=1", ()
    if kind == _WITH_UNOWNED:
        return "owner IN (?, '')", (owner_key,)
    return "owner=?", (owner_key,)


def _prefer_own(kind: str, owner_key: str, rows: list) -> list:
    """In an own-plus-unowned view (a developer), the caller's own row of a
    call also pending unowned: the caller's own call, not a guess between
    owners. Every other view, the operator's included, keeps all the rows."""
    if kind != _WITH_UNOWNED:
        return rows
    own = [row for row in rows if str(row["owner"]) == owner_key]
    return own or rows


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


class AmbiguousCallError(ApprovalLedgerError):
    """A call id that names more than one call, or one call pending for more
    than one owner; the message lists the references that each name one row."""

    ambiguous = True


def call_id(digest: str) -> str:
    """The short form an operator types: the first 16 hex characters."""
    return str(digest or "")[:CALL_ID_CHARS]


def escape_preview(text: str) -> str:
    """``text`` with every control, format (bidi, zero-width) and non-ASCII-space
    separator character written as a ``\\u`` escape.

    A preview reaches the operator's terminal through ``/approvals``; a raw
    escape sequence or bidi override in it could rewrite the listing and pass
    a harmful pending call off as a benign one. Idempotent: the escapes it
    writes are printable ASCII.
    """
    out = []
    for ch in str(text or ""):
        if ch != " " and unicodedata.category(ch)[0] in "CZ":
            code = ord(ch)
            out.append("\\u%04x" % code if code <= 0xFFFF else "\\U%08x" % code)
        else:
            out.append(ch)
    return "".join(out)


def preview_token(text: str) -> str:
    """``text`` bare when it is one plain printable-ASCII token, else JSON-quoted."""
    if _BARE_PREVIEW_TOKEN.match(text):
        return text
    return json.dumps(text, ensure_ascii=True)


def preview_complete(preview: str) -> bool:
    """Whether a stored preview shows everything its call's digest covers
    (bulk payloads, shown by length only, excepted)."""
    return not str(preview or "").startswith(_INCOMPLETE_HEAD)


def _incomplete_mark(reasons: list[str]) -> str:
    return PREVIEW_INCOMPLETE % ", ".join(reasons) if reasons else ""


def _finish_preview(body: str, reasons: list[str], limit: int) -> str:
    """``body`` escaped, cut to ``limit`` and led by the incomplete mark when
    any of ``reasons`` (or the cut) hid part of it."""
    body = escape_preview(body)
    if len(_incomplete_mark(reasons)) + len(body) > limit:
        reasons = reasons + ["truncated"]
        keep = max(0, limit - len(_incomplete_mark(reasons)) - len(PREVIEW_TRUNCATED))
        body = body[:keep] + PREVIEW_TRUNCATED
    return _incomplete_mark(reasons) + body


def render_preview(fields: Sequence[PreviewField], *,
                   redact: Callable[[str], str] | None = None,
                   limit: int = MAX_PREVIEW_CHARS) -> str:
    """One ``key=value`` line from ``fields``.

    Each field is redacted as one ``key=value`` unit, so a pattern that needs
    the key to recognise a credential (``password=...``, ``api_key: ...``)
    still sees it, while a redaction that eats the rest of its text
    (``Cookie: ...``) stops at the end of that field. A field the redactor left
    alone is shown as its key and value, each quoted when not one plain token.
    A redacted field whose key survived shows the key and the redactor's view
    of the value; one whose key the redactor rewrote is shown as the redacted
    unit, quoted whole. A redaction that changed anything, a failing redactor
    or a cut leads the line with ``PREVIEW_INCOMPLETE``; a failing redactor
    writes no field text at all.
    """
    reasons: list[str] = []
    parts = []
    for raw_key, raw_value, literal in fields:
        key, value = str(raw_key), str(raw_value)
        unit = "%s=%s" % (key, value)
        cleaned = unit
        if redact is not None:
            try:
                cleaned = str(redact(unit))
            except Exception:
                return _finish_preview("", ["redaction failed"], limit)
        if cleaned == unit:
            parts.append("%s=%s" % (
                preview_token(key), value if literal else preview_token(value),
            ))
            continue
        if "redacted" not in reasons:
            reasons.append("redacted")
        head = key + "="
        if cleaned.startswith(head):
            # A literal the redactor cut is no longer whole JSON; quote it.
            parts.append("%s=%s" % (preview_token(key), preview_token(cleaned[len(head):])))
        else:
            parts.append(preview_token(cleaned))
    return _finish_preview(" ".join(parts), reasons, limit)


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
    def complete(self) -> bool:
        """The preview shows everything an approval of this call covers."""
        return preview_complete(self.preview)

    @property
    def ref(self) -> str:
        """The reference that names exactly this row (see ``call_ref``)."""
        return call_ref(self.digest, self.owner)

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
    owner: str = ""
    # Set only on the value ``issue`` returns: it cleared a standing denial.
    overrode_denial: bool = field(default=False, compare=False)

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

    def scope(self, owner: str = "", *, include_unowned: bool = False,
              all_owners: bool = False) -> contextlib.AbstractContextManager[None]:
        """Bind the principal owner scope for approval ledger calls (see ``owner_scope``)."""
        return owner_scope(owner, include_unowned=include_unowned, all_owners=all_owners)

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
                    # One statement at a time: ``executescript`` would COMMIT
                    # the BEGIN IMMEDIATE above first, so a racing upgrader
                    # could see the table gone between DROP and RENAME, and a
                    # crash there would leave pending_calls_v2 orphaned. As
                    # separate statements the rebuild is one transaction, and
                    # an orphan a pre-fix build left behind is dropped first.
                    conn.execute("DROP TABLE IF EXISTS pending_calls_v2")
                    conn.execute(
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
                        )
                        """
                    )
                    conn.execute(
                        """
                        INSERT INTO pending_calls_v2
                            (owner, digest, tool, surface, preview, first_ts, last_ts, count, detail, denied_ts, detail_chars)
                        SELECT '', digest, tool, surface, preview, first_ts, last_ts, count, detail, denied_ts, detail_chars
                        FROM pending_calls
                        """
                    )
                    conn.execute("DROP TABLE pending_calls")
                    conn.execute("ALTER TABLE pending_calls_v2 RENAME TO pending_calls")
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

    def _clean_preview(self, preview: Preview) -> str:
        """The stored form of ``preview``: argument fields rendered by
        ``render_preview``, free text redacted as one line; either way led by
        ``PREVIEW_INCOMPLETE`` when redaction or a cut hid part of it."""
        if not isinstance(preview, str):
            return render_preview(list(preview or ()), redact=self._redact)
        text = preview
        if not text:
            return ""
        reasons: list[str] = []
        if self._redact is not None:
            try:
                cleaned = str(self._redact(text))
            except Exception:
                # A redactor that fails must not write the raw text.
                return _finish_preview("", ["redaction failed"], MAX_PREVIEW_CHARS)
            if cleaned != text:
                reasons.append("redacted")
            text = cleaned
        if text.endswith(PREVIEW_TRUNCATED):
            reasons.append("truncated")
        if reasons and not preview_complete(text):
            reasons = []  # already led by the mark
        return _finish_preview(text, reasons, MAX_PREVIEW_CHARS)

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
                       preview: Preview = "", detail: str = "", owner: str | None = None) -> PendingCall:
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
        """The one pending row ``call_id_or_digest`` names.

        The reference is a digest prefix (the call id), optionally qualified by
        an owner tag, ``<call id>@<owner tag>`` (``@-`` names the unowned key).
        A call pending for more than one owner in this view (the same tool and
        arguments) raises ``AmbiguousCallError`` listing each row's reference:
        the owner is never guessed. A developer's own-plus-unowned view
        resolves a bare id to the developer's own row (``_prefer_own``).
        """
        prefix, tag = _split_ref(call_id_or_digest)
        if len(prefix) < MIN_CALL_ID_CHARS or any(c not in "0123456789abcdef" for c in prefix):
            raise ApprovalLedgerError(
                "a call id is at least %d hex characters (see /approvals)" % MIN_CALL_ID_CHARS
            )
        if tag is not None and not tag:
            raise ApprovalLedgerError("an owner tag follows '@' (see /approvals)")
        kind, owner_key = _read_scope(owner, include_unowned)
        where, params = _owner_sql(kind, owner_key)
        if tag == UNOWNED_TAG:
            where += " AND owner=''"
        elif tag is not None:
            where, params = where + " AND substr(owner, 1, ?)=?", (*params, len(tag), tag)
        with self._read() as conn:
            digests = conn.execute(
                "SELECT DISTINCT digest FROM pending_calls WHERE " + where
                + " AND digest LIKE ? LIMIT 2",
                (*params, prefix + "%"),
            ).fetchall()
            if not digests:
                raise ApprovalLedgerError("no pending call starts with %r%s" % (
                    prefix, "" if tag is None else " for owner %s" % tag))
            if len(digests) > 1:
                raise AmbiguousCallError("call id %r is ambiguous; give more characters" % prefix)
            rows = conn.execute(
                "SELECT * FROM pending_calls WHERE " + where
                + " AND digest=? ORDER BY last_ts DESC LIMIT 20",
                (*params, str(digests[0]["digest"])),
            ).fetchall()
        rows = _prefer_own(kind, owner_key, rows)
        if len(rows) > 1:
            raise AmbiguousCallError(
                "call %s is pending for more than one owner; name one: %s" % (
                    call_id(str(rows[0]["digest"])),
                    ", ".join(call_ref(str(r["digest"]), str(r["owner"])) for r in rows))
            )
        return self._pending(rows[0])

    def pending(self, limit: int = 20, *, owner: str | None = None,
                include_unowned: bool | None = None) -> list[PendingCall]:
        kind, owner_key = _read_scope(owner, include_unowned)
        where, params = _owner_sql(kind, owner_key)
        with self._read() as conn:
            rows = conn.execute(
                "SELECT * FROM pending_calls WHERE " + where + " ORDER BY last_ts DESC LIMIT ?",
                (*params, max(1, min(int(limit), 200))),
            ).fetchall()
        return [self._pending(row) for row in rows]

    # -- approvals ---------------------------------------------------------

    def issue(self, tool: str, digest: str, *, approver: str, surface: str = "",
              ttl_seconds: int = DEFAULT_TTL_SECONDS, preview: Preview = "",
              override_denial: bool = False, owner: str | None = None) -> Approval:
        """Approve exactly one future call of ``tool`` with argument ``digest``.

        Raises ``ApprovalDeniedError`` while an operator's denial of this call
        stands (``DENIAL_TTL_SECONDS``); the check runs in the issuing
        transaction, so it serialises with ``deny``. ``override_denial=True``
        clears the denial and issues in that same transaction; only the
        console ``/approve`` command passes it.

        The approval belongs to the owner whose pending call it answers, so
        that owner's next unchanged call spends it. A caller that resolved a
        pending row passes that row's ``owner``. Without one, in a view wider
        than one owner (the operator, a developer), it is the one owner in view
        with this call pending (a developer's own row first); more than one
        raises ``AmbiguousCallError`` instead of guessing. With no pending call to answer it belongs to the
        caller's own owner. The returned approval has ``overrode_denial`` set
        when it cleared a standing denial.
        """
        tool = str(tool or "").strip().lstrip("/")
        digest = str(digest or "").strip().lower()
        approver = str(approver or "").strip()
        if not tool or len(digest) != 64:
            raise ApprovalLedgerError("an approval needs a tool and a full digest")
        if not approver:
            raise ApprovalLedgerError("an approval must name who approved it")
        if owner is None:
            kind, owner_key = _read_scope(None, None)
        else:
            kind, owner_key = _EXACT, str(owner or "").strip()
        ttl = clamp_ttl(ttl_seconds)
        now = time.time()
        nonce = "apv_" + secrets.token_hex(8)
        clean = self._clean_preview(preview)
        with self._write() as conn:
            self._prune(conn, now)
            if kind != _EXACT:
                where, params = _owner_sql(kind, owner_key)
                matches = conn.execute(
                    "SELECT owner FROM pending_calls WHERE " + where + " AND digest=? LIMIT 20",
                    (*params, digest),
                ).fetchall()
                matches = _prefer_own(kind, owner_key, matches)
                if len(matches) > 1:
                    raise AmbiguousCallError(
                        "call %s is pending for more than one owner; approve one by its "
                        "reference: %s" % (call_id(digest), ", ".join(
                            call_ref(digest, str(m["owner"])) for m in matches))
                    )
                if matches:
                    owner_key = str(matches[0]["owner"])
            overrode = False
            row = conn.execute(
                "SELECT preview, denied_ts FROM pending_calls WHERE owner=? AND digest=?",
                (owner_key, digest),
            ).fetchone()
            if (row is not None and row["denied_ts"] is not None
                    and float(row["denied_ts"]) > now - DENIAL_TTL_SECONDS):
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
                overrode = True
            stored = str(row["preview"]) if row is not None else ""
            # The pending call's own preview (``/approve <call id>``) is
            # already clean; running the redactor over it again could eat
            # more of it.
            if not clean or (isinstance(preview, str) and preview == stored):
                clean = stored
            conn.execute(
                """
                INSERT INTO approvals (nonce, owner, tool, digest, approver, surface, preview,
                                       issued_ts, expires_ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (nonce, owner_key, tool, digest, approver, str(surface or ""), clean, now, now + ttl),
            )
            row = conn.execute("SELECT * FROM approvals WHERE nonce=?", (nonce,)).fetchone()
        return replace(self._approval(row), overrode_denial=overrode)

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
        clear it. A caller that already resolved the row passes its digest and
        ``owner``, so the denial lands on exactly that row.
        """
        pending = self.resolve_call(
            call_id_or_digest, owner=owner, include_unowned=include_unowned,
        )
        target_owner = pending.owner
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
        kind, owner_key = _read_scope(owner, include_unowned)
        owner_sql, owner_params = _owner_sql(kind, owner_key)
        now = time.time()
        with self._write() as conn:
            cursor = conn.execute(
                f"""
                UPDATE approvals SET revoked_ts=?
                WHERE nonce=? AND {owner_sql} AND consumed_ts IS NULL AND revoked_ts IS NULL AND expires_ts > ?
                """,
                (now, nonce, *owner_params, now),
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
        kind, owner_key = _read_scope(owner, include_unowned)
        owner_sql, owner_params = _owner_sql(kind, owner_key)
        now = time.time()
        with self._read() as conn:
            if include_spent:
                rows = conn.execute(
                    f"SELECT * FROM approvals WHERE {owner_sql} ORDER BY issued_ts DESC LIMIT ?",
                    (*owner_params, max(1, min(int(limit), 200))),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"""
                    SELECT * FROM approvals
                    WHERE {owner_sql} AND consumed_ts IS NULL AND revoked_ts IS NULL AND expires_ts > ?
                    ORDER BY issued_ts DESC LIMIT ?
                    """,
                    (*owner_params, now, max(1, min(int(limit), 200))),
                ).fetchall()
        return [self._approval(row) for row in rows]

    # -- presentation ------------------------------------------------------

    def format_status(self, limit: int = 20) -> str:
        """The operator's view: what asked, what is approved, what was spent."""
        now = time.time()
        pending = self.pending(limit)
        approvals = self.approvals(include_spent=True, limit=limit)
        lines = ["one-shot approvals"]
        lines.append("  pending calls (refused unattended; approve one with /approve <call ref>,")
        lines.append("  or with /approve call <tool> <json> when its preview says text is not shown):")
        if not pending:
            lines.append("    (none)")
        for item in pending:
            lines.append("    %s  %s  x%d  via %s  last %s  %s" % (
                item.ref, item.tool, item.count, item.surface or "-",
                _stamp(item.last_ts), escape_preview(item.preview),
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
            owner=str(row["owner"] or "") if "owner" in row.keys() else "",
        )


def default_ledger() -> ApprovalLedger:
    """The production ledger: the configured home, previews redacted."""
    from ...platform.logging import Redactor

    return ApprovalLedger(redact=Redactor().redact)


__all__ = [
    "AmbiguousCallError", "Approval", "ApprovalDeniedError", "ApprovalLedger",
    "ApprovalLedgerError", "DEFAULT_TTL_SECONDS", "DENIAL_TTL_SECONDS",
    "MAX_DETAIL_CHARS", "MAX_TTL_SECONDS", "MIN_TTL_SECONDS", "PREVIEW_INCOMPLETE",
    "PendingCall", "Preview", "PreviewField", "call_id", "call_ref", "clamp_ttl",
    "current_owner", "default_ledger", "escape_preview", "operator_scope",
    "operator_view", "owner_scope", "owner_tag", "preview_complete",
    "preview_token", "render_preview",
]
