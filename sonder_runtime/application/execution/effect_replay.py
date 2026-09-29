"""Tamper-evidence and mock-replay views over the effect journal.

Two read-only contracts sit beside the #515 intent/outcome journal:

* ``ChainVerification`` is the result of walking the journal's hash chain.
  Every journal write appends one chain record holding the canonical row
  content, the previous record's hash and its own hash.  Verification reports
  the first broken link: an edited, deleted or inserted journal row, or an
  edited, deleted or reordered chain record.  Rows written before the chain
  existed form a *legacy segment* pinned by the chain's anchor record; they
  are accepted as long as they still match the snapshot the anchor pinned.

* ``ToolResponseReplay`` lets a later run substitute recorded tool responses
  instead of re-invoking tools ("mock replay").  It is built from the
  journal's per-effect response records in sequence order and states, per
  position, whether the recorded response is substitutable or why it is not
  (unresolved effect, no response captured, digest only, content that does
  not match its digest, or a sequence gap).

Neither contract changes checkpoint or high-water semantics: both only read.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Iterator, Protocol

from sonder_runtime.application.execution.effect_journal import (
    EffectIntent, EffectJournalError, EffectState,
)


@dataclass(frozen=True, slots=True)
class ChainBreak:
    """One point where the journal no longer matches its hash chain.

    ``chain_seq`` is the chain record at (or just before) the break; it is
    ``None`` for a journal row that has no chain record at all.
    """

    chain_seq: int | None
    intent_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class ChainVerification:
    ok: bool
    records_checked: int
    legacy_rows: int
    head_seq: int
    head_hash: str
    first_break: ChainBreak | None = None
    breaks: tuple[ChainBreak, ...] = ()


@dataclass(frozen=True, slots=True)
class RecordedResponseRow:
    """A journal row joined with its captured tool response, if any.

    ``response_digest`` is empty when no response was captured.
    ``response_json`` is ``None`` when only the digest was kept.
    """

    intent: EffectIntent
    response_digest: str = ""
    response_json: str | None = None
    response_bytes: int = 0


class RecordedResponseSource(Protocol):
    def recorded_responses(
        self, run_id: str, *, after_sequence: int = 0, limit: int = 100,
    ) -> tuple[tuple[RecordedResponseRow, ...], bool]: ...


@dataclass(frozen=True, slots=True)
class RecordedToolResponse:
    sequence: int
    intent_id: str
    operation_id: str
    idempotency_key: str
    request_digest: str
    state: EffectState
    receipt_key: str
    response_digest: str
    response: object


@dataclass(frozen=True, slots=True)
class MissingToolResponse:
    sequence: int
    intent_id: str
    reason: str


class ReplayResponseMissing(EffectJournalError):
    """The next replay position has no substitutable recorded response."""


class ReplayDivergence(EffectJournalError):
    """The replaying run asked for a different request than was recorded."""


MISSING_UNRESOLVED = "unresolved"
MISSING_NOT_RECORDED = "response_not_recorded"
MISSING_CONTENT = "content_not_recorded"
MISSING_DIGEST_MISMATCH = "content_digest_mismatch"
MISSING_SEQUENCE_GAP = "sequence_gap"


def response_digest(response_json: str) -> str:
    return hashlib.sha256(response_json.encode("utf-8")).hexdigest()


class ToolResponseReplay:
    """Ordered recorded responses of one run, for mock replay.

    Iterating yields the substitutable responses in journal sequence order.
    ``missing`` lists every position that cannot be substituted, with a
    reason.  ``substitute`` walks all positions in order and refuses at the
    first missing one or at a request that differs from the recorded one, so
    a replay never silently skips or reorders an effect.
    """

    def __init__(self, run_id: str, entries: tuple[RecordedToolResponse | MissingToolResponse, ...]) -> None:
        self.run_id = run_id
        self._entries = entries
        self.responses = tuple(e for e in entries if isinstance(e, RecordedToolResponse))
        self.missing = tuple(e for e in entries if isinstance(e, MissingToolResponse))
        self._cursor = 0

    @property
    def complete(self) -> bool:
        return not self.missing

    @property
    def entries(self) -> tuple[RecordedToolResponse | MissingToolResponse, ...]:
        return self._entries

    def __iter__(self) -> Iterator[RecordedToolResponse]:
        return iter(self.responses)

    def __len__(self) -> int:
        return len(self._entries)

    def substitute(self, request_digest: str | None = None) -> RecordedToolResponse:
        if self._cursor >= len(self._entries):
            raise ReplayResponseMissing("replay exhausted: no further recorded effect")
        entry = self._entries[self._cursor]
        if isinstance(entry, MissingToolResponse):
            raise ReplayResponseMissing(
                f"recorded response missing at sequence {entry.sequence}: {entry.reason}"
            )
        if request_digest is not None and request_digest != entry.request_digest:
            raise ReplayDivergence(
                f"replay request diverged at sequence {entry.sequence}"
            )
        self._cursor += 1
        return entry


def _entry_for(row: RecordedResponseRow) -> RecordedToolResponse | MissingToolResponse:
    intent = row.intent
    if intent.state in {EffectState.INTENT, EffectState.UNCERTAIN}:
        return MissingToolResponse(intent.sequence, intent.intent_id, MISSING_UNRESOLVED)
    if not row.response_digest:
        return MissingToolResponse(intent.sequence, intent.intent_id, MISSING_NOT_RECORDED)
    if row.response_json is None:
        return MissingToolResponse(intent.sequence, intent.intent_id, MISSING_CONTENT)
    if response_digest(row.response_json) != row.response_digest:
        return MissingToolResponse(intent.sequence, intent.intent_id, MISSING_DIGEST_MISMATCH)
    try:
        response = json.loads(row.response_json)
    except ValueError:
        return MissingToolResponse(intent.sequence, intent.intent_id, MISSING_DIGEST_MISMATCH)
    return RecordedToolResponse(
        intent.sequence, intent.intent_id, intent.operation_id, intent.idempotency_key,
        intent.request_digest, intent.state, intent.receipt_key, row.response_digest, response,
    )


def build_tool_response_replay(
    source: RecordedResponseSource, run_id: str, *, page_size: int = 500,
    max_records: int = 100_000,
) -> ToolResponseReplay:
    """Read every recorded effect of ``run_id`` in order into a replay plan."""
    if not isinstance(run_id, str) or not run_id.strip():
        raise EffectJournalError("run_id is required")
    if type(page_size) is not int or not 1 <= page_size <= 10_000:
        raise EffectJournalError("page_size must be within 1..10000")
    entries: list[RecordedToolResponse | MissingToolResponse] = []
    after = 0
    while True:
        rows, truncated = source.recorded_responses(run_id, after_sequence=after, limit=page_size)
        for row in rows:
            sequence = row.intent.sequence
            for gap in range(after + 1, sequence):
                entries.append(MissingToolResponse(gap, "", MISSING_SEQUENCE_GAP))
            entries.append(_entry_for(row))
            after = sequence
            if len(entries) > max_records:
                raise EffectJournalError("tool response replay exceeds bounded size")
        if not truncated or not rows:
            break
    return ToolResponseReplay(run_id, tuple(entries))


__all__ = [
    "ChainBreak", "ChainVerification", "MISSING_CONTENT", "MISSING_DIGEST_MISMATCH",
    "MISSING_NOT_RECORDED", "MISSING_SEQUENCE_GAP", "MISSING_UNRESOLVED",
    "MissingToolResponse", "RecordedResponseRow", "RecordedResponseSource",
    "RecordedToolResponse", "ReplayDivergence", "ReplayResponseMissing",
    "ToolResponseReplay", "build_tool_response_replay", "response_digest",
]
