"""Conversation turns for export and listing: the session event log, plus the
interaction rows written before that log began.

The memory ``interactions`` table is the learning corpus: only learning-route
turns (and MCP web-routed replies) write to it.  ``user.message`` /
``model.response`` / ``model.failed`` events are committed for every
model-backed turn, so once a session's log holds a turn the log is the
transcript.  Memory semantics are untouched; export and listing only read.

Rows and logged turns share no identifier and their text differs (capture
redacts; the row keeps the raw prompt), so a row is never matched to a logged
turn by content.  A row is kept only when it provably predates every logged
turn (``_pre_log_rows``); every other row is dropped, because a row the log may
hold is either a duplicate or, when retention withheld its turn, a
resurrection.  Any failure to read, verify or interpret the log raises
``SessionTranscriptUnavailable``: there is no fallback to the raw rows.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from ...domain.common.errors import IntegrityFailure

_RETENTION_MARKER = "session.retention.applied"
_PROVIDER_EVIDENCE = frozenset({"provider.requested", "provider.responded", "provider.failed"})
# A retrospective capture (no provider.* event) is appended after its turn's
# interaction row; this bounds how much earlier that row can be.
_UNLINKED_CAPTURE_GUARD = timedelta(hours=1)
_MAX_COUNTED_SESSIONS = 100


class SessionTranscriptUnavailable(IntegrityFailure):
    """The session event log could not be read, verified or interpreted."""


def _payload(event: Any) -> Mapping[str, Any]:
    payload = getattr(event, "payload", None)
    return payload if isinstance(payload, Mapping) else {}


def _privacy_targets(events: Iterable[Any]) -> dict[int, str]:
    """``sequence -> privacy_class`` from retention markers; malformed -> raise.

    Same validation as ``SessionQueryEngine._privacy_targets``.
    """
    targets: dict[int, str] = {}
    for event in events:
        if event.event_type != _RETENTION_MARKER:
            continue
        raw = _payload(event).get("targets")
        if not isinstance(raw, list):
            raise SessionTranscriptUnavailable("invalid session privacy marker")
        for target in raw:
            if not isinstance(target, Mapping):
                raise SessionTranscriptUnavailable("invalid session privacy target")
            sequence, label = target.get("sequence"), target.get("privacy_class")
            if (isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1
                    or not isinstance(label, str)):
                raise SessionTranscriptUnavailable("invalid session privacy target")
            targets[sequence] = label
    return targets


def turns_from_events(events: Iterable[Any],
                      withheld: Mapping[int, str] | None = None) -> list[dict[str, object]]:
    """Group events into ``{id, task, response, ts, tier}`` turns, oldest first.

    A turn opens at its ``user.message``; its ``model.response`` (the last one
    wins) supplies ``response`` and a ``model.failed`` with no response is
    rendered as ``[no response: <code>]`` so a failed turn is still counted.
    ``withheld`` maps a sequence to its retention class: that event's text
    renders as ``[redacted: <class>]`` and a withheld user turn carries no id.
    """
    withheld = withheld or {}
    turns: list[dict[str, object]] = []
    by_turn: dict[str, dict[str, object]] = {}
    tiers: dict[str, str] = {}  # model.requested is committed before user.message
    for event in events:
        payload = _payload(event)
        turn_id = str(payload.get("turn_id") or "")
        seq = getattr(event, "sequence", None)
        label = withheld.get(seq) if isinstance(seq, int) and not isinstance(seq, bool) else None
        text = ("[redacted: %s]" % (label or "withheld") if label is not None
                else str(payload.get("content") or ""))
        if event.event_type == "user.message":
            turn: dict[str, object] = {
                "id": "" if label is not None else (turn_id or event.event_id),
                "task": text, "response": None, "ts": event.occurred_at_utc,
                "tier": tiers.get(turn_id, ""),
            }
            turns.append(turn)
            if turn_id:
                by_turn[turn_id] = turn
        elif event.event_type == "model.requested":
            tiers[turn_id] = "" if label is not None else str(payload.get("tier") or "")
        elif event.event_type == "model.response" and turn_id in by_turn:
            by_turn[turn_id]["response"] = text
        elif event.event_type == "model.failed" and turn_id in by_turn:
            by_turn[turn_id].setdefault(
                "failure", "withheld" if label is not None else str(payload.get("error_code") or "error"))
    for turn in turns:
        failure = str(turn.pop("failure", "unanswered"))
        if turn["response"] is None:
            turn["response"] = "[no response: %s]" % failure
    return turns


def _utc(value: object) -> datetime | None:
    """An ISO-8601 or SQLite ``YYYY-MM-DD HH:MM:SS`` (UTC) timestamp, else None."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace(" ", "T", 1)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _pre_log_rows(events: Sequence[Any],
                  rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The rows that provably predate every logged user turn, in their order.

    The anchor is the first ``model.requested``/``user.message`` of a turn that
    has a ``user.message``.  A turn admitted at provider dispatch (it has a
    ``provider.*`` event) writes its row after that anchor, so the row of any
    logged turn falls in the anchor's second or later.  A retrospective
    capture is appended after its row: when the first turn names its row as
    ``request_id`` that row is excluded by id, otherwise rows within
    ``_UNLINKED_CAPTURE_GUARD`` before the anchor are excluded as well.  A row
    named by any logged ``request_id``, or with no parseable ``ts``, is never
    kept.
    """
    user_turns = {
        tid for e in events
        if e.event_type == "user.message"
        for tid in (str(_payload(e).get("turn_id") or ""),)
        if tid
    }
    first = next(
        (
            e for e in events
            if e.event_type == "user.message"
            or (
                e.event_type == "model.requested"
                and str(_payload(e).get("turn_id") or "") in user_turns
            )
        ),
        None,
    )
    if first is None:
        return list(rows)
    anchor = _utc(first.occurred_at_utc)
    if anchor is None:
        return []
    first_turn = str(_payload(first).get("turn_id") or "")
    linked = {str(_payload(e)["request_id"]) for e in events if _payload(e).get("request_id")}
    first_ids = {str(_payload(e)["request_id"]) for e in events
                 if first_turn and _payload(e).get("turn_id") == first_turn
                 and _payload(e).get("request_id")}
    admitted = bool(first_turn) and any(
        e.event_type in _PROVIDER_EVIDENCE and _payload(e).get("turn_id") == first_turn
        for e in events)
    cutoff = anchor.replace(microsecond=0)
    if not admitted and not any(str(row.get("id") or "") in first_ids for row in rows):
        cutoff -= _UNLINKED_CAPTURE_GUARD
    kept: list[Mapping[str, Any]] = []
    for row in rows:
        moment = _utc(row.get("ts"))
        if moment is not None and moment < cutoff and str(row.get("id") or "") not in linked:
            kept.append(row)
    return kept


def repository_of(application: Any) -> Any:
    """The application's session repository, or None when it has no such capability.

    Only a missing capability means "no log"; a repository that exists but
    fails to open raises (and ``fuller_turns`` fails closed).
    """
    getter = getattr(application, "session_repository", None)
    return None if getter is None else getter()


def _verified_events(get_repository: Callable[[], Any], session_id: str) -> tuple[Any, ...] | None:
    """The complete, chain-verified log; None only when no repository exists."""
    try:
        repository = get_repository()
        if repository is None:
            return None
        return tuple(repository.read_complete(session_id))
    except Exception as error:  # integrity, read bound, payload bound, I/O
        raise SessionTranscriptUnavailable(
            "session event log could not be read and verified") from error


def fuller_turns(get_repository: Callable[[], Any], session_id: str,
                 interaction_turns: Sequence[Mapping[str, Any]]) -> Sequence[Mapping[str, Any]]:
    """Pre-log interaction rows, then the event-log turns, oldest first.

    Rows need ``id`` and ``ts`` (``memory_store.session_turn_rows``).  Withheld
    turn bodies render as a placeholder.  ``interaction_turns`` itself is
    returned when there is no repository or the log holds no user turn, so a
    caller can tell the cases apart.  Raises ``SessionTranscriptUnavailable``
    when the log cannot be read, verified or interpreted.
    """
    events = _verified_events(get_repository, session_id)
    if events is None:
        return interaction_turns
    logged = turns_from_events(events, _privacy_targets(events))
    if not logged:
        return interaction_turns
    return [*_pre_log_rows(events, interaction_turns), *logged]


def merged_turn_counts(get_repository: Callable[[], Any],
                       interaction_rows: Callable[[str], Sequence[Mapping[str, Any]]],
                       session_ids: Iterable[str]) -> dict[str, int]:
    """``len(fuller_turns(...))`` per session: the count the export would list.

    A session whose log cannot be verified, or past the first
    ``_MAX_COUNTED_SESSIONS``, is omitted; the caller shows it as unknown.
    """
    counts: dict[str, int] = {}
    for session_id in list(session_ids)[:_MAX_COUNTED_SESSIONS]:
        try:
            counts[session_id] = len(fuller_turns(get_repository, session_id,
                                                  interaction_rows(session_id)))
        except Exception:
            continue
    return counts


__all__ = [
    "SessionTranscriptUnavailable",
    "fuller_turns",
    "merged_turn_counts",
    "repository_of",
    "turns_from_events",
]
