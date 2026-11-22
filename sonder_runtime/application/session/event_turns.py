"""Conversation turns read from the durable session event log.

The memory ``interactions`` table is the learning corpus: only turns on the
learning route write to it, so a selected-model or cloud turn never appears
there.  ``user.message`` / ``model.response`` / ``model.failed`` events are
committed for every model-backed turn, so this is the complete transcript.
Memory semantics are untouched; export and listing merely read the log.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from types import SimpleNamespace
from typing import Any

from .query_export import retention_envelope

_RETENTION_MARKER = "session.retention.applied"
_WITHHOLDABLE = frozenset({"user.message", "model.response"})


def turns_from_events(events: Iterable[Any]) -> list[dict[str, object]]:
    """Group events into ``{id, task, response, ts, tier}`` turns, oldest first.

    A turn opens at its ``user.message``; its ``model.response`` (the last one
    wins) supplies ``response`` and a ``model.failed`` with no response is
    rendered as ``[no response: <code>]`` so a failed turn is still counted.
    """
    turns: list[dict[str, object]] = []
    by_turn: dict[str, dict[str, object]] = {}
    tiers: dict[str, str] = {}  # model.requested is committed before user.message
    for event in events:
        payload = event.payload if isinstance(event.payload, Mapping) else {}
        turn_id = str(payload.get("turn_id") or "")
        if event.event_type == "user.message":
            turn: dict[str, object] = {
                "id": turn_id or event.event_id, "task": str(payload.get("content") or ""),
                "response": None, "ts": event.occurred_at_utc,
                "tier": tiers.get(turn_id, ""),
            }
            turns.append(turn)
            if turn_id:
                by_turn[turn_id] = turn
        elif event.event_type == "model.requested":
            tiers[turn_id] = str(payload.get("tier") or "")
        elif event.event_type == "model.response" and turn_id in by_turn:
            by_turn[turn_id]["response"] = str(payload.get("content") or "")
        elif event.event_type == "model.failed" and turn_id in by_turn:
            by_turn[turn_id].setdefault("failure", str(payload.get("error_code") or "error"))
    for turn in turns:
        if turn["response"] is None:
            turn["response"] = "[no response: %s]" % turn.pop("failure", "unanswered")
    return turns


def _retention_aware(events: Iterable[Any]) -> list[Any]:
    """The events with every retention-withheld turn body replaced by a placeholder.

    Retention only appends ``session.retention.applied`` markers; it never edits
    history, so a raw read still holds the withheld text.  This applies the same
    ``sequence -> privacy_class`` targets ``SessionQueryEngine`` applies and fails
    closed (``ValueError``) on a malformed marker.  Only ``content`` is replaced;
    the opaque ``turn_id`` stays so the turn still groups and still counts.
    """
    events = list(events)
    targets: dict[int, str] = {}
    for event in events:
        if event.event_type != _RETENTION_MARKER:
            continue
        raw = event.payload.get("targets") if isinstance(event.payload, Mapping) else None
        if not isinstance(raw, list):
            raise ValueError("invalid session privacy marker")
        for target in raw:
            sequence = target.get("sequence") if isinstance(target, Mapping) else None
            label = target.get("privacy_class") if isinstance(target, Mapping) else None
            if isinstance(sequence, bool) or not isinstance(sequence, int) or not isinstance(label, str):
                raise ValueError("invalid session privacy target")
            targets[sequence] = label
    result: list[Any] = []
    for event in events:
        sequence = getattr(event, "sequence", None)
        label = targets.get(sequence) if isinstance(sequence, int) else None
        if label is None or event.event_type not in _WITHHOLDABLE:
            result.append(event)
            continue
        placeholder = "[redacted: %s]" % (retention_envelope(label)["privacy_class"] or "withheld")
        payload = {"turn_id": event.payload.get("turn_id"), "content": placeholder}
        result.append(SimpleNamespace(event_type=event.event_type, payload=payload,
                                      event_id=event.event_id,
                                      occurred_at_utc=event.occurred_at_utc))
    return result


def _unmatched(interaction_turns: Sequence[Mapping[str, Any]],
               logged_tasks: Iterable[str]) -> list[Mapping[str, Any]]:
    """Interaction rows the event log does not already hold (matched by prompt text)."""
    pending = Counter(task.strip() for task in logged_tasks)
    kept: list[Mapping[str, Any]] = []
    for row in interaction_turns:
        key = str(row.get("task") or "").strip()
        if pending[key] > 0:
            pending[key] -= 1
        else:
            kept.append(row)
    return kept


def fuller_turns(get_repository: Callable[[], Any], session_id: str,
                 interaction_turns: Sequence[Mapping[str, Any]]) -> Sequence[Mapping[str, Any]]:
    """Interaction rows the log lacks, then the event-log turns, oldest first.

    A row that the log already holds (same prompt text) is dropped as a duplicate;
    a row the log never saw (a turn from before the log existed) is kept, so
    neither source hides turns the other holds.  Withheld turn bodies render as a
    placeholder.  The repository is fetched lazily, so a runtime without one reads
    as "no log"; when the log is unreadable or empty the argument itself is
    returned, so a caller can tell the two cases apart.
    """
    try:
        events = list(get_repository().read_complete(session_id))
    except Exception:
        return interaction_turns
    if not events:
        return interaction_turns
    raw_tasks = [str(e.payload.get("content") or "") for e in events
                 if e.event_type == "user.message" and isinstance(e.payload, Mapping)]
    try:
        logged = turns_from_events(_retention_aware(events))
    except Exception:
        # Fail closed on every event-logged turn when retention validation or
        # event projection fails: keep only genuinely pre-log interaction rows,
        # never fall back to raw SQLite copies of event-logged (potentially
        # retention-withheld) turns.
        return _unmatched(interaction_turns, raw_tasks)
    if not logged:
        return _unmatched(interaction_turns, raw_tasks) if raw_tasks else interaction_turns
    return [*_unmatched(interaction_turns, raw_tasks), *logged]


def merged_turn_counts(get_repository: Callable[[], Any],
                       interaction_rows: Callable[[str], Sequence[Mapping[str, Any]]],
                       session_ids: Iterable[str]) -> dict[str, int]:
    """Merged turn count per session; a session whose log cannot be read is omitted.

    ``interaction_rows`` returns a session's interaction rows.  Retention never
    changes a count, so the user messages are read unredacted.
    """
    counts: dict[str, int] = {}
    for session_id in session_ids:
        try:
            users = get_repository().search(
                session_id=session_id, event_type="user.message", limit=10_000)
            tasks = [str(e.payload.get("content") or "") for e in users]
            counts[session_id] = len(users) + len(_unmatched(interaction_rows(session_id), tasks))
        except Exception:
            continue
    return counts


__all__ = ["fuller_turns", "merged_turn_counts", "turns_from_events"]
