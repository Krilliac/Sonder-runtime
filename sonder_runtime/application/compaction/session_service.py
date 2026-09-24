"""Durable session compaction application service."""
from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
from typing import Any
from uuid import uuid4

from .legacy import CompactionApplicationService, canonical_summary
from ..compaction_retention import (
    REFERENCE_KEYS,
    SUMMARY_SCHEMA_VERSION,
    canonical_bytes,
    critical_retention_problems,
    is_critical,
)
from ..ports.compaction import (
    CompactionEvent,
    CompactionEngine,
    CompactionValidationError,
    CompactionResult,
    CompactionSummary,
    CompactionRequest,
    CompactionValidation,
    SessionHistoryEvent,
    SourceRange,
)
from ..ports.session_repository import SessionEvent, SessionRepository
from ..session.archive import ArchivedContext, SessionContextArchiveService


class SessionCompactionError(ValueError):
    """Raised when durable session history cannot satisfy a compaction request."""


@dataclass(frozen=True, slots=True)
class CompactedMatch:
    """A raw source event found by search, with the summary covering it."""

    event: SessionEvent
    compaction_event_id: str | None


_RECALLED_FIELDS = ("decisions", "facts", "unresolved_tasks", "artifacts", "tool_outcomes")


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class SessionCompactionService:
    """Read, validate, compact, and append one durable session event."""

    def __init__(
        self,
        repository: SessionRepository,
        *,
        engine: CompactionEngine | None = None,
        event_id_factory: Callable[[], str] | None = None,
        max_events: int = 1_000,
        archive_service: SessionContextArchiveService | None = None,
        max_scan_events: int = 100_000,
    ) -> None:
        if isinstance(max_events, bool) or max_events < 1:
            raise ValueError("max_events must be positive")
        if isinstance(max_scan_events, bool) or not isinstance(max_scan_events, int) \
                or max_scan_events < 1:
            raise ValueError("max_scan_events must be positive")
        self._max_scan_events = max_scan_events
        self._repository = repository
        # Keep the repository orchestration independent from the summarizer.
        # Production may inject a different typed engine; the deterministic
        # engine remains the safe default for the current runtime.
        self._engine = engine or CompactionApplicationService(
            event_id_factory=event_id_factory,
        )
        self._event_id_factory = event_id_factory or (lambda: f"compaction-{uuid4().hex}")
        self._max_events = max_events
        self._archive = archive_service or SessionContextArchiveService(repository)

    def archive_context(
        self,
        session_id: str,
        *,
        start_sequence: int = 1,
        end_sequence: int | None = None,
        budget_bytes: int,
    ) -> ArchivedContext:
        """Prepare a bounded model context through the durable compaction seam.

        This is intentionally explicit: the provider-facing caller supplies
        the already assembled session event range and measured byte budget.
        The archive service appends references before returning placeholders;
        no live provider request is changed implicitly by this application
        service.
        """
        if end_sequence is None:
            # Probe one event beyond the service bound.  Without this probe a
            # longer session could be mistaken for a complete prefix and its
            # later failure/decision history would disappear from context.
            probe_limit = self._max_events + 1
            adapter_limit = getattr(self._repository, "_max_read_limit", probe_limit)
            if isinstance(adapter_limit, int) and not isinstance(adapter_limit, bool):
                probe_limit = min(probe_limit, adapter_limit)
            events = self._repository.read_range(
                session_id, start_sequence=start_sequence, limit=probe_limit,
            )
            if len(events) > self._max_events:
                raise SessionCompactionError("source range exceeds the service bound")
            if probe_limit <= self._max_events and len(events) == probe_limit:
                raise SessionCompactionError("source range tail cannot be proven within adapter bound")
        else:
            if end_sequence < start_sequence:
                raise SessionCompactionError("source range must be non-empty and ordered")
            count = end_sequence - start_sequence + 1
            if count > self._max_events:
                raise SessionCompactionError("source range exceeds the service bound")
            events = self._repository.read_range(
                session_id, start_sequence=start_sequence,
                end_sequence=end_sequence, limit=count,
            )
        if not events or events[0].sequence != start_sequence:
            raise SessionCompactionError("source range is unavailable or truncated")
        if end_sequence is not None and events[-1].sequence != end_sequence:
            raise SessionCompactionError("source range is unavailable or truncated")
        try:
            return self._archive.prepare_context(
                session_id, events, budget_bytes=budget_bytes,
            )
        except (ValueError, TypeError) as exc:
            raise SessionCompactionError(str(exc)) from exc

    def archive_verified_context(
        self,
        session_id: str,
        events: Sequence[SessionEvent],
        *,
        budget_bytes: int,
    ) -> ArchivedContext:
        """Archive an already chain-verified, complete event snapshot.

        The caller (the live lane) obtained ``events`` from
        ``SessionRepository.read_complete``, which bounds and verifies the
        whole history in one read transaction. Re-reading the range here
        would reintroduce an unverified second read between verification and
        provider assembly, so the verified tuple is archived as-is. The
        archive service still enforces its own item bound and ordering.
        """
        values = tuple(events)
        if not values or values[0].sequence != 1:
            raise SessionCompactionError("verified context must start at the session head")
        if any(right.sequence != left.sequence + 1 for left, right in zip(values, values[1:])):
            raise SessionCompactionError("verified context must be contiguous")
        try:
            return self._archive.prepare_context(
                session_id, values, budget_bytes=budget_bytes,
            )
        except (ValueError, TypeError) as exc:
            raise SessionCompactionError(str(exc)) from exc

    def compact(
        self,
        session_id: str,
        *,
        start_sequence: int,
        end_sequence: int,
        max_summary_tokens: int | None = None,
    ) -> SessionEvent:
        if not session_id.strip():
            raise SessionCompactionError("session_id is required")
        if (
            isinstance(start_sequence, bool)
            or isinstance(end_sequence, bool)
            or start_sequence < 1
            or end_sequence < start_sequence
        ):
            raise SessionCompactionError("source range must be non-empty and ordered")
        count = end_sequence - start_sequence + 1
        if count > self._max_events:
            raise SessionCompactionError("source range exceeds the service bound")
        events = self._repository.read_range(
            session_id,
            start_sequence=start_sequence,
            end_sequence=end_sequence,
            limit=count,
        )
        if len(events) != count or tuple(event.sequence for event in events) != tuple(
            range(start_sequence, end_sequence + 1)
        ):
            raise SessionCompactionError("source range is truncated or non-contiguous")
        if any(event.session_id != session_id for event in events):
            raise SessionCompactionError("source range contains a different session")
        source = SourceRange(
            session_id,
            start_sequence,
            end_sequence,
            events[0].event_id,
            events[-1].event_id,
        )
        request = CompactionRequest(
            session_id,
            tuple(self._history_event(event) for event in events),
            source,
            max_summary_tokens=max_summary_tokens,
        )
        try:
            result = self._engine.compact(request)
            validation = self._engine.validate(request, result)
        except CompactionValidationError as exc:
            raise SessionCompactionError(str(exc)) from exc
        if not validation.valid:
            raise SessionCompactionError(validation.detail)
        summary = result.summary
        # Independent deterministic gate: whatever engine produced the
        # summary, it may not drop a failure, constraint, requirement, or
        # structured value from the range it replaces in live context.
        problems = critical_retention_problems(request.history, summary)
        if problems:
            raise SessionCompactionError(
                "compaction summary omits critical history: " + "; ".join(problems[:8])
            )
        payload = {
            "summary_schema": SUMMARY_SCHEMA_VERSION,
            "source_range": {
                "session_id": source.session_id,
                "start_sequence": source.start_sequence,
                "end_sequence": source.end_sequence,
                "start_event_id": source.start_event_id,
                "end_event_id": source.end_event_id,
            },
            "summary": {
                "facts": list(summary.facts),
                "decisions": list(summary.decisions),
                "unresolved_tasks": list(summary.unresolved_tasks),
                "artifacts": list(summary.artifacts),
                "tool_outcomes": list(summary.tool_outcomes),
                "confidence": summary.confidence,
                "modalities": [
                    {
                        "event_id": item.event_id,
                        "event_type": item.event_type,
                        "modality": item.modality,
                        "payload": _json_value(item.payload),
                    }
                    for item in summary.modalities
                ],
            },
        }
        return self._repository.append(
            session_id,
            "compaction.completed",
            payload,
            event_id=result.appended_event.event_id,
        )

    def validate_persisted_event(
        self,
        event: SessionEvent,
        source_events: Sequence[SessionEvent],
    ) -> CompactionSummary:
        """Validate an append-only compaction event before live replay.

        The source events remain the authority.  A persisted summary is merely
        a provider-facing replacement after its exact range, typed modalities,
        and engine retention validation all succeed.
        """
        try:
            if event.event_type != "compaction.completed":
                raise SessionCompactionError("persisted event is not compaction.completed")
            payload = event.payload
            source = payload.get("source_range")
            raw_summary = payload.get("summary")
            if not isinstance(source, Mapping) or not isinstance(raw_summary, Mapping):
                raise SessionCompactionError("persisted compaction event is incomplete")
            required_source = {
                "session_id", "start_sequence", "end_sequence",
                "start_event_id", "end_event_id",
            }
            if set(source) != required_source:
                raise SessionCompactionError("persisted compaction source range is malformed")
            if source["session_id"] != event.session_id:
                raise SessionCompactionError("persisted compaction session changed")
            start = source["start_sequence"]
            end = source["end_sequence"]
            if (
                isinstance(start, bool) or isinstance(end, bool)
                or not isinstance(start, int) or not isinstance(end, int)
                or start < 1 or end < start or end - start + 1 > self._max_events
                or event.sequence <= end
            ):
                raise SessionCompactionError("persisted compaction source range is invalid")
            values = tuple(source_events)
            expected = tuple(range(start, end + 1))
            if (
                len(values) != len(expected)
                or tuple(item.sequence for item in values) != expected
                or values[0].event_id != source["start_event_id"]
                or values[-1].event_id != source["end_event_id"]
                or any(item.session_id != event.session_id for item in values)
                or any(item.event_type == "compaction.completed" for item in values)
            ):
                raise SessionCompactionError("persisted compaction source range is incomplete")
            required_summary = {
                "facts", "decisions", "unresolved_tasks", "artifacts",
                "tool_outcomes", "confidence", "modalities",
            }
            if set(raw_summary) != required_summary:
                raise SessionCompactionError("persisted compaction summary is incomplete")
            raw_modalities = raw_summary["modalities"]
            if not isinstance(raw_modalities, (list, tuple)):
                raise SessionCompactionError("persisted compaction modalities are malformed")
            modalities = []
            for item in raw_modalities:
                if not isinstance(item, Mapping) or set(item) != {
                    "event_id", "event_type", "modality", "payload",
                }:
                    raise SessionCompactionError("persisted compaction modality is malformed")
                if any(
                    not isinstance(item[field], str) or not item[field].strip()
                    for field in ("event_id", "event_type", "modality")
                ):
                    raise SessionCompactionError("persisted compaction modality identity is malformed")
                if not isinstance(item["payload"], Mapping):
                    raise SessionCompactionError("persisted compaction modality payload is malformed")
                modalities.append(SessionHistoryEvent(
                    item["event_id"], event.session_id, 0,
                    item["event_type"], dict(item["payload"]),
                    item["modality"],
                ))
            summary = CompactionSummary(
                facts=raw_summary["facts"], decisions=raw_summary["decisions"],
                unresolved_tasks=raw_summary["unresolved_tasks"],
                artifacts=raw_summary["artifacts"],
                tool_outcomes=raw_summary["tool_outcomes"],
                modalities=tuple(modalities),
                confidence=raw_summary["confidence"],
            )
            source_range = SourceRange(
                event.session_id, start, end,
                source["start_event_id"], source["end_event_id"],
            )
            request = CompactionRequest(
                event.session_id,
                tuple(self._history_event(item) for item in values),
                source_range,
            )
            if type(self._engine) is not CompactionApplicationService:
                raise SessionCompactionError(
                    "persisted compaction requires the deterministic engine"
                )
            schema = payload.get("summary_schema", 1)
            if isinstance(schema, bool) or schema not in (1, SUMMARY_SCHEMA_VERSION):
                raise SessionCompactionError("persisted compaction summary schema is unsupported")
            canonical = canonical_summary(request, schema=schema)

            def projection(value: CompactionSummary):
                return (
                    tuple(value.facts), tuple(value.decisions),
                    tuple(value.unresolved_tasks), tuple(value.artifacts),
                    tuple(value.tool_outcomes), value.confidence,
                    tuple(
                        (
                            item.event_id, item.event_type, item.modality,
                            dict(item.payload),
                        )
                        for item in value.modalities
                    ),
                )

            if projection(summary) != projection(canonical):
                raise SessionCompactionError(
                    "persisted compaction summary differs from canonical source summary"
                )
            problems = critical_retention_problems(request.history, summary)
            if problems and schema == 1:
                # An authentic summary written before critical retention (it
                # matched the schema-1 canonical projection above) may have
                # collapsed a constrained message.  The persisted event is only
                # a marker binding the range; replay the lossless schema-2
                # projection re-derived from the same original events instead.
                # No re-compaction is needed (and a second summary over the
                # same range would be rejected by lane replay as an overlap).
                summary = canonical_summary(request, schema=SUMMARY_SCHEMA_VERSION)
                problems = critical_retention_problems(request.history, summary)
            if problems:
                raise SessionCompactionError(
                    "persisted compaction summary omits critical history: "
                    + "; ".join(problems[:8])
                )
            candidate = CompactionResult(
                event.session_id, source_range, summary,
                CompactionEvent(event.event_id, event.session_id, source_range, summary),
                CompactionValidation(True),
            )
            validation = self._engine.validate(request, candidate)
            if not validation.valid:
                raise SessionCompactionError(validation.detail or "persisted compaction failed validation")
            for field in ("facts", "decisions", "unresolved_tasks", "artifacts", "tool_outcomes"):
                expected_values = []
                for source_event in values:
                    raw_values = source_event.payload.get(field, ())
                    if isinstance(raw_values, str):
                        raw_values = (raw_values,)
                    if isinstance(raw_values, (list, tuple)):
                        expected_values.extend(
                            value for value in raw_values
                            if isinstance(value, str) and value.strip()
                        )
                missing = set(expected_values) - set(getattr(summary, field))
                if missing:
                    raise SessionCompactionError(
                        "persisted compaction summary omitted " + field
                    )
            return summary
        except SessionCompactionError:
            raise
        except (CompactionValidationError, TypeError, ValueError, KeyError) as exc:
            raise SessionCompactionError("persisted compaction event is malformed") from exc

    def _read_bound(self) -> int:
        adapter_limit = getattr(self._repository, "_max_read_limit", self._max_events)
        if isinstance(adapter_limit, bool) or not isinstance(adapter_limit, int):
            return self._max_events
        return max(1, min(self._max_events, adapter_limit))

    def _complete_search(self, session_id: str, **filters) -> tuple[SessionEvent, ...] | None:
        """One bounded repository search, or ``None`` when it may be truncated.

        The repository search is oldest-first with a row limit, so a full page
        cannot prove that newer rows were not cut off.
        """
        bound = self._read_bound()
        rows = self._repository.search(session_id=session_id, limit=bound, **filters)
        return tuple(rows) if len(rows) < bound else None

    def _scan(self, session_id: str):
        """Every event of the session in sequence order, keyset-paged.

        Fails closed past ``max_scan_events`` instead of silently stopping, so
        a caller can never mistake a partial scan for a complete one.
        """
        page_size = self._read_bound()
        start, scanned = 1, 0
        while True:
            page = self._repository.read_range(
                session_id, start_sequence=start, limit=page_size,
            )
            if not page:
                return
            scanned += len(page)
            if scanned > self._max_scan_events:
                raise SessionCompactionError("session exceeds the compaction scan bound")
            yield from page
            if len(page) < page_size:
                return
            start = page[-1].sequence + 1

    def _summaries(self, session_id: str) -> tuple[SessionEvent, ...]:
        rows = self._complete_search(session_id, event_type="compaction.completed")
        if rows is None:
            rows = tuple(
                event for event in self._scan(session_id)
                if event.event_type == "compaction.completed"
            )
        return rows

    def _compaction_event(self, session_id: str, compaction_event_id: str) -> SessionEvent:
        if not isinstance(compaction_event_id, str) or not compaction_event_id.strip():
            raise SessionCompactionError("compaction event id is required")
        # Event identities are row metadata, not payload text; match on the
        # complete set of summaries (bounded search, or a keyset scan when
        # the search could have cut off the newest rows).
        for candidate in self._summaries(session_id):
            if candidate.event_id == compaction_event_id:
                return candidate
        raise SessionCompactionError("compaction event is unavailable")

    def recover_source(
        self, session_id: str, compaction_event_id: str,
    ) -> tuple[SessionEvent, ...]:
        """Return the exact, validated original events a summary replaced.

        This is the lossless side of compaction: after a restart the covered
        range is re-read from the append-only log and the persisted summary is
        re-validated against it before anything is returned.  When the
        repository offers ``read_complete`` (the chain-verified snapshot the
        live lane uses), the range is sliced from that verified snapshot, so a
        row altered out of band fails integrity instead of being returned.
        """
        event = self._compaction_event(session_id, compaction_event_id)
        source = event.payload.get("source_range")
        if not isinstance(source, Mapping):
            raise SessionCompactionError("persisted compaction source range is malformed")
        start, end = source.get("start_sequence"), source.get("end_sequence")
        if (
            isinstance(start, bool) or isinstance(end, bool)
            or not isinstance(start, int) or not isinstance(end, int)
            or start < 1 or end < start or end - start + 1 > self._max_events
        ):
            raise SessionCompactionError("persisted compaction source range is invalid")
        read_complete = getattr(self._repository, "read_complete", None)
        if callable(read_complete):
            try:
                verified = read_complete(
                    session_id, max_events=min(self._max_scan_events, 100_000),
                )
            except (TypeError, ValueError) as exc:
                raise SessionCompactionError(
                    "session history is unavailable or failed integrity verification"
                ) from exc
            events = tuple(item for item in verified if start <= item.sequence <= end)
        else:
            events = self._repository.read_range(
                session_id, start_sequence=start, end_sequence=end, limit=end - start + 1,
            )
        self.validate_persisted_event(event, events)
        return tuple(events)

    def recall_critical(
        self, session_id: str, compaction_event_id: str,
    ) -> tuple[SessionEvent, ...]:
        """Decisions, failures, constraints, and facts under one summary.

        Every returned event is an original, re-validated source event, so its
        identity and sequence are the provenance of the recalled item.
        """
        return tuple(
            event for event in self.recover_source(session_id, compaction_event_id)
            if is_critical(self._history_event(event))
            or any(event.payload.get(field) for field in _RECALLED_FIELDS)
        )

    def retrieve_reference(
        self, session_id: str, reference: Mapping[str, object],
    ) -> Mapping[str, object]:
        """Recover a bulky payload a summary replaced by a digest reference.

        ``reference`` is the retained modality payload (its ``reference_*``
        keys); the original event is re-read and its identity and digest are
        verified before the payload is returned.
        """
        if not isinstance(reference, Mapping) or any(key not in reference for key in REFERENCE_KEYS):
            raise SessionCompactionError("compaction reference is malformed")
        sequence = reference["reference_sequence"]
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
            raise SessionCompactionError("compaction reference sequence is invalid")
        events = self._repository.read_range(
            session_id, start_sequence=sequence, end_sequence=sequence, limit=1,
        )
        if len(events) != 1:
            raise SessionCompactionError("referenced source event is unavailable")
        event = events[0]
        encoded = canonical_bytes(_json_value(event.payload))
        if (
            event.event_id != reference["reference_event_id"]
            or event.event_type != reference["reference_event_type"]
            or len(encoded) != reference["reference_byte_count"]
            or hashlib.sha256(encoded).hexdigest() != reference["reference_sha256"]
        ):
            raise SessionCompactionError("referenced source event identity or digest changed")
        return dict(event.payload)

    def search_compacted(
        self, session_id: str, query: str, *, limit: int = 20,
    ) -> tuple[CompactedMatch, ...]:
        """Search raw history, marking which summary (if any) covers a match.

        Compaction never removes source events, so summarized or evicted
        material stays searchable by its original text.  ``query`` is a
        literal, case-sensitive substring of the stored canonical payload:
        SQL ``LIKE`` wildcards (``%``, ``_``) have no special meaning.  The
        repository ``LIKE`` search is used only as a superset prefilter; when
        it could be truncated (a full page, possibly filled by summary or
        non-literal rows) the session is keyset-scanned instead.  Returns the
        newest ``limit`` source matches, newest first; the covering summary is
        the newest one whose range includes the match.
        """
        if not isinstance(query, str) or not query.strip():
            raise SessionCompactionError("query must be non-empty")
        needle = query.strip()
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= self._max_events:
            raise SessionCompactionError("limit is out of bounds")
        ranges = []
        for event in self._summaries(session_id):
            source = event.payload.get("source_range")
            if isinstance(source, Mapping):
                start, end = source.get("start_sequence"), source.get("end_sequence")
                if (
                    isinstance(start, int) and isinstance(end, int)
                    and not isinstance(start, bool) and not isinstance(end, bool)
                ):
                    ranges.append((event.sequence, start, end, event.event_id))
        ranges.sort(reverse=True)
        candidates = self._complete_search(session_id, text=needle)
        if candidates is None:
            candidates = self._scan(session_id)
        # Candidates arrive oldest-first (search rows and scan pages are both
        # in sequence order), so a bounded deque retains exactly the newest
        # ``limit`` matches without holding every match of a broad query.
        newest: deque[SessionEvent] = deque(maxlen=limit)
        for event in candidates:
            if event.event_type in {"compaction.completed", "context.archive.created"}:
                continue
            stored = json.dumps(
                _json_value(event.payload), ensure_ascii=False,
                sort_keys=True, separators=(",", ":"),
            )
            if needle in stored:
                newest.append(event)
        literal = sorted(newest, key=lambda event: event.sequence, reverse=True)
        return tuple(
            CompactedMatch(
                event,
                next(
                    (event_id for _, start, end, event_id in ranges
                     if start <= event.sequence <= end),
                    None,
                ),
            )
            for event in literal
        )

    @staticmethod
    def _history_event(event: SessionEvent) -> SessionHistoryEvent:
        payload = dict(event.payload)
        modality = payload.get("modality", "text")
        if not isinstance(modality, str) or not modality.strip():
            modality = "text"
        return SessionHistoryEvent(
            event.event_id,
            event.session_id,
            event.sequence,
            event.event_type,
            payload,
            modality,
        )


__all__ = ["CompactedMatch", "SessionCompactionError", "SessionCompactionService"]
