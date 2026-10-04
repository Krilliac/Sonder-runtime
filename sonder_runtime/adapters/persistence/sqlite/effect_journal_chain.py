"""Hash chain and tool-response records for the SQLite effect journal.

The ``effect_journal`` table is mutable by design (an intent row moves to a
terminal or uncertain state in place), so the chain is kept in a separate,
append-only table.  Every journal write appends one ``row`` record holding the
canonical content of the row *after* that write:

    row_hash = sha256("{chain_seq}\\n{kind}\\n{intent_id}\\n{prev_hash}\\n{content}")

``chain_seq`` is contiguous from 1, so a deleted or reordered chain record
breaks the walk.  Record 1 is always the ``anchor``.  Journal rows that existed
when the chain was introduced are never rewritten: the anchor pins them as a
legacy segment by count and by a digest over ``effect_journal_legacy``, which
holds one content hash per legacy row captured at migration time.

A journal row is consistent when its current canonical content equals its
latest chained record, or, if it was never written since the migration, its
legacy snapshot.  Anything else is a break: an edited row, a deleted row, or
a row inserted without going through the journal.

The chain makes tampering *evident*, not impossible: someone able to rewrite
the whole database can rebuild a consistent chain.  ``chain_head`` returns the
current head so a caller can record it somewhere else.
"""
from __future__ import annotations

import hashlib
import json

from sonder_runtime.application.execution.effect_replay import ChainBreak, ChainVerification

CHAIN_FORMAT = "sonder.effect_journal.chain/1"
GENESIS_HASH = "0" * 64
MAX_REPORTED_BREAKS = 100

CHAIN_DDL = """
CREATE TABLE IF NOT EXISTS effect_journal_chain (
    chain_seq INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    intent_id TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    row_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS effect_journal_legacy (
    intent_id TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS effect_tool_response (
    intent_id TEXT PRIMARY KEY,
    response_digest TEXT NOT NULL,
    response_bytes INTEGER NOT NULL,
    response_json TEXT
);
"""

ROW_COLUMNS = (
    "intent_id", "run_id", "worker_id", "operation_id", "scope", "owner_epoch",
    "idempotency_key", "request_digest", "reconciliation", "sequence", "state",
    "outcome_digest", "receipt_key", "detail",
)
_INT_COLUMNS = frozenset({"owner_epoch", "sequence"})

_ROW_WITH_RESPONSE = (
    "SELECT j.intent_id,j.run_id,j.worker_id,j.operation_id,j.scope,j.owner_epoch,"
    "j.idempotency_key,j.request_digest,j.reconciliation,j.sequence,j.state,"
    "j.outcome_digest,j.receipt_key,j.detail,COALESCE(r.response_digest,'') "
    "FROM effect_journal j LEFT JOIN effect_tool_response r ON r.intent_id=j.intent_id"
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_row(values) -> str:
    """Canonical JSON of the 14 journal columns plus the response digest."""
    document: dict[str, object] = {}
    for name, value in zip(ROW_COLUMNS, values[:len(ROW_COLUMNS)], strict=True):
        document[name] = int(value) if name in _INT_COLUMNS else str(value)
    document["response_digest"] = str(values[len(ROW_COLUMNS)])
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def link_hash(chain_seq: int, kind: str, intent_id: str, prev_hash: str, content: str) -> str:
    return _sha(f"{chain_seq}\n{kind}\n{intent_id}\n{prev_hash}\n{content}")


def _legacy_digest(pairs) -> str:
    digest = hashlib.sha256()
    for intent_id, content_hash in pairs:
        digest.update(f"{intent_id}\x00{content_hash}\n".encode("utf-8"))
    return digest.hexdigest()


def is_initialised(connection) -> bool:
    """Read-only: whether all chain tables exist and the chain has a record."""
    tables = {
        str(row[0]) for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
            "('effect_journal_chain','effect_journal_legacy','effect_tool_response')"
        )
    }
    if len(tables) != 3:
        return False
    return connection.execute("SELECT 1 FROM effect_journal_chain LIMIT 1").fetchone() is not None


def ensure_chain(connection) -> None:
    """Create the chain tables and anchor pre-existing rows exactly once.

    Runs in the caller's ``BEGIN IMMEDIATE`` transaction.  Existing journal
    rows are only read.  An anchor is written only while both the chain and
    the legacy table are empty, so a deleted anchor is never silently
    re-created over existing chain state.
    """
    for statement in CHAIN_DDL.split(";"):
        if statement.strip():
            connection.execute(statement)
    if connection.execute("SELECT 1 FROM effect_journal_chain LIMIT 1").fetchone() is not None:
        return
    if connection.execute("SELECT 1 FROM effect_journal_legacy LIMIT 1").fetchone() is not None:
        return
    pairs = []
    for row in connection.execute(_ROW_WITH_RESPONSE + " ORDER BY j.intent_id"):
        pairs.append((str(row[0]), _sha(canonical_row(row))))
    connection.executemany(
        "INSERT INTO effect_journal_legacy(intent_id,content_hash) VALUES(?,?)", pairs,
    )
    content = json.dumps(
        {"format": CHAIN_FORMAT, "legacy_rows": len(pairs), "legacy_digest": _legacy_digest(pairs)},
        sort_keys=True, separators=(",", ":"),
    )
    connection.execute(
        "INSERT INTO effect_journal_chain(chain_seq,kind,intent_id,content,prev_hash,row_hash) "
        "VALUES(1,'anchor','',?,?,?)",
        (content, GENESIS_HASH, link_hash(1, "anchor", "", GENESIS_HASH, content)),
    )


def append_row(connection, intent_id: str) -> None:
    """Chain the current content of one journal row inside the caller's write."""
    row = connection.execute(_ROW_WITH_RESPONSE + " WHERE j.intent_id=?", (intent_id,)).fetchone()
    if row is None:
        return
    content = canonical_row(row)
    head = connection.execute(
        "SELECT chain_seq,row_hash FROM effect_journal_chain ORDER BY chain_seq DESC LIMIT 1"
    ).fetchone()
    chain_seq, prev_hash = (1, GENESIS_HASH) if head is None else (int(head[0]) + 1, str(head[1]))
    connection.execute(
        "INSERT INTO effect_journal_chain(chain_seq,kind,intent_id,content,prev_hash,row_hash) "
        "VALUES(?,'row',?,?,?,?)",
        (chain_seq, intent_id, content, prev_hash,
         link_hash(chain_seq, "row", intent_id, prev_hash, content)),
    )


def record_response(connection, intent_id: str, encoded: tuple[str, int, str | None]) -> None:
    digest, size, content = encoded
    connection.execute(
        "INSERT OR REPLACE INTO effect_tool_response(intent_id,response_digest,response_bytes,response_json) "
        "VALUES(?,?,?,?)",
        (intent_id, digest, size, content),
    )


def encode_response(value: object, *, keep_content: bool, max_bytes: int) -> tuple[str, int, str | None] | None:
    """Canonical JSON digest of a tool response; never raises.

    Uses the gateway's canonical form (sorted keys, compact separators,
    ``default=str``).  ``None`` means the value could not be encoded and no
    response record is written; the effect outcome itself is unaffected.
    """
    try:
        text = json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError):
        return None
    size = len(text.encode("utf-8"))
    kept = text if keep_content and size <= max_bytes else None
    return _sha(text), size, kept


def chain_head(connection) -> tuple[int, str]:
    head = connection.execute(
        "SELECT chain_seq,row_hash FROM effect_journal_chain ORDER BY chain_seq DESC LIMIT 1"
    ).fetchone()
    return (0, GENESIS_HASH) if head is None else (int(head[0]), str(head[1]))


def verify(connection) -> ChainVerification:
    """Walk the chain, then cross-check the journal and response tables."""
    breaks: list[ChainBreak] = []
    expected, prev_hash, checked = 1, GENESIS_HASH, 0
    anchor: dict | None = None
    latest: dict[str, tuple[int, str]] = {}
    for chain_seq, kind, intent_id, content, stored_prev, stored_hash in connection.execute(
        "SELECT chain_seq,kind,intent_id,content,prev_hash,row_hash FROM effect_journal_chain "
        "ORDER BY chain_seq"
    ):
        chain_seq, kind, intent_id = int(chain_seq), str(kind), str(intent_id)
        content, stored_prev, stored_hash = str(content), str(stored_prev), str(stored_hash)
        reason = None
        if chain_seq != expected:
            reason = f"chain record {expected} is missing (deleted or reordered)"
        elif stored_prev != prev_hash:
            reason = "prev_hash does not link to the previous chain record"
        elif link_hash(chain_seq, kind, intent_id, stored_prev, content) != stored_hash:
            reason = "row_hash does not match the record content (record edited)"
        elif (chain_seq == 1) != (kind == "anchor") or kind not in {"anchor", "row"}:
            reason = "chain record kind is out of place"
        if reason is not None:
            break_seq = expected if chain_seq != expected else chain_seq
            first = ChainBreak(break_seq, intent_id, reason)
            head_seq, head_hash = chain_head(connection)
            return ChainVerification(False, checked, 0, head_seq, head_hash, first, (first,))
        checked += 1
        prev_hash, expected = stored_hash, expected + 1
        if kind == "anchor":
            anchor = json.loads(content)
        else:
            latest[intent_id] = (chain_seq, _sha(content))
    head_seq, head_hash = expected - 1, prev_hash
    if anchor is None:
        first = ChainBreak(1, "", "chain anchor record is missing")
        return ChainVerification(False, checked, 0, head_seq, head_hash, first, (first,))
    legacy_pairs = [
        (str(row[0]), str(row[1])) for row in connection.execute(
            "SELECT intent_id,content_hash FROM effect_journal_legacy ORDER BY intent_id"
        )
    ]
    if (
        anchor.get("format") != CHAIN_FORMAT
        or anchor.get("legacy_rows") != len(legacy_pairs)
        or anchor.get("legacy_digest") != _legacy_digest(legacy_pairs)
    ):
        first = ChainBreak(1, "", "legacy segment does not match the chain anchor")
        return ChainVerification(False, checked, len(legacy_pairs), head_seq, head_hash, first, (first,))
    legacy = dict(legacy_pairs)
    seen: set[str] = set()
    for row in connection.execute(_ROW_WITH_RESPONSE):
        intent_id = str(row[0])
        seen.add(intent_id)
        content_hash = _sha(canonical_row(row))
        if intent_id in latest:
            chain_seq, chained_hash = latest[intent_id]
            if content_hash != chained_hash:
                breaks.append(ChainBreak(chain_seq, intent_id, "journal row differs from its chained record (edited)"))
        elif intent_id in legacy:
            if content_hash != legacy[intent_id]:
                breaks.append(ChainBreak(1, intent_id, "legacy row differs from its anchored snapshot (edited)"))
        else:
            breaks.append(ChainBreak(None, intent_id, "journal row has no chain record (inserted outside the journal)"))
    for intent_id, (chain_seq, _hash) in latest.items():
        if intent_id not in seen:
            breaks.append(ChainBreak(chain_seq, intent_id, "chained journal row is missing (deleted)"))
    for intent_id in legacy:
        if intent_id not in seen and intent_id not in latest:
            breaks.append(ChainBreak(1, intent_id, "legacy journal row is missing (deleted)"))
    for intent_id, digest, content in connection.execute(
        "SELECT intent_id,response_digest,response_json FROM effect_tool_response "
        "WHERE response_json IS NOT NULL"
    ):
        if _sha(str(content)) != str(digest):
            position = latest.get(str(intent_id), (None, ""))[0]
            breaks.append(ChainBreak(position, str(intent_id), "recorded response content does not match its digest"))
    breaks.sort(key=lambda item: (item.chain_seq is None, item.chain_seq or 0, item.intent_id))
    reported = tuple(breaks[:MAX_REPORTED_BREAKS])
    return ChainVerification(
        not breaks, checked, len(legacy_pairs), head_seq, head_hash,
        reported[0] if reported else None, reported,
    )


__all__ = [
    "CHAIN_DDL", "CHAIN_FORMAT", "GENESIS_HASH", "append_row", "canonical_row", "chain_head",
    "encode_response", "ensure_chain", "is_initialised", "link_hash", "record_response", "verify",
]
