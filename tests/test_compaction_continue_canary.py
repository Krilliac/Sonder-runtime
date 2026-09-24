"""Issue #510 canary: compaction-and-continue never silently loses critical history.

A session is seeded with planted requirements, constraints, decisions (both
structured and in model rationale), failed attempts (tool, model, and a bulky
failing tool result), and bulky successful tool output.  The range is
compacted, the session continues, the process "restarts" (every repository and
service is reopened from disk), and each planted item must still be
retrievable -- from the provider-facing summary and, with provenance, from the
lossless append-only archive.  Bulky output must leave live context by
digest-bound reference and remain recoverable.
"""
from __future__ import annotations

import json

import pytest

from sonder_runtime.adapters.persistence.agent_lanes import SQLiteAgentLaneStore
from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository
from sonder_runtime.application.agents.interactive_lanes import AgentLaneService
from sonder_runtime.application.compaction import (
    CompactionApplicationService,
    SessionCompactionError,
    SessionCompactionService,
)
from sonder_runtime.application.compaction.legacy import canonical_summary
from sonder_runtime.application.compaction.session_service import _json_value
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.compaction import (
    CompactionEvent,
    CompactionRequest,
    CompactionResult,
    CompactionSummary,
    CompactionValidation,
    SessionHistoryEvent,
    SourceRange,
)
from sonder_runtime.application.ports.model_gateway import ModelResponse


SESSION = "canary-session"

# Every token here must survive compaction-and-continue.
CRITICAL_TOKENS = (
    "REQ-offline-only",
    "CONSTRAINT-no-network",
    "DECISION-use-sqlite",
    "FACT-schema-v3",
    "DECISION-retry-with-backoff",
    "FAILURE-attempt-1-linker",
    "FAILURE-attempt-2-timeout",
    "FAILURE-model-timeout",
)
BULKY_TOKENS = ("LINKER-NOISE", "BULKY-OK")


def _plant(repo) -> list:
    """Append the planted history; return the source events in order."""
    return [
        repo.append(SESSION, "message.received", {
            "text": "Build the exporter",
            "requirements": ["REQ-offline-only"],
            "constraints": ["CONSTRAINT-no-network"],
        }, event_id="req-1"),
        repo.append(SESSION, "message.sent", {
            "text": "Plan accepted",
            "decisions": ["DECISION-use-sqlite"],
            "facts": ["FACT-schema-v3"],
        }, event_id="decision-1"),
        repo.append(SESSION, "model.response", {
            "content": "Rationale: DECISION-retry-with-backoff because attempt 1 timed out",
        }, event_id="rationale-1"),
        repo.append(SESSION, "tool.completed", {
            "call_id": "a1", "tool": "build", "exit_code": 2,
            "error": "FAILURE-attempt-1-linker",
            "output": "LINKER-NOISE " * 800,
        }, event_id="attempt-1"),
        repo.append(SESSION, "tool.failed", {
            "call_id": "a2", "error": "FAILURE-attempt-2-timeout",
        }, event_id="attempt-2"),
        repo.append(SESSION, "model.failed", {
            "error_code": "timeout", "detail": "FAILURE-model-timeout",
        }, event_id="model-failure"),
        repo.append(SESSION, "tool.result", {
            "call_id": "a3", "content": "BULKY-OK " * 3000,
        }, event_id="bulky-ok"),
        repo.append(SESSION, "message.sent", {"text": "small talk"}, event_id="chat-1"),
    ]


def _summary_text(event) -> str:
    return json.dumps(event.payload["summary"], ensure_ascii=False, sort_keys=True)


def _references(summary: CompactionSummary) -> dict[str, dict]:
    return {
        item.event_id: dict(item.payload)
        for item in summary.modalities
        if "reference_sha256" in item.payload
    }


def test_compaction_and_continue_retains_every_planted_item_after_restart(tmp_path):
    path = tmp_path / "sessions.db"
    repo = SQLiteSessionRepository(path)
    planted = _plant(repo)
    original = {event.event_id: event.event_hash for event in planted}

    compacted = SessionCompactionService(
        repo, event_id_factory=lambda: "compaction-1",
    ).compact(SESSION, start_sequence=1, end_sequence=len(planted))
    assert compacted.payload["summary_schema"] == 2

    # Continue the session after compaction, then restart from disk.
    repo.append(SESSION, "message.received", {"text": "continue with attempt 3"},
                event_id="continue-1")
    repo.append(SESSION, "tool.failed", {"call_id": "a4", "error": "FAILURE-attempt-3"},
                event_id="attempt-3")
    assert repo.close() is True
    reopened = SQLiteSessionRepository(path)
    service = SessionCompactionService(reopened)

    # The raw archive is untouched (non-destructive, COMPACT-001).
    source = reopened.read_range(SESSION, start_sequence=1, end_sequence=len(planted),
                                 limit=len(planted))
    assert {event.event_id: event.event_hash for event in source} == original
    assert reopened.inspect_integrity(SESSION).valid

    persisted = reopened.search(session_id=SESSION, event_type="compaction.completed",
                                limit=10)
    assert [event.event_id for event in persisted] == ["compaction-1"]
    summary = service.validate_persisted_event(persisted[0], source)

    # 1. Provider-facing summary still carries every critical item.
    text = _summary_text(persisted[0])
    for token in CRITICAL_TOKENS:
        assert token in text, f"{token} silently lost by compaction"
    assert summary.decisions == ("DECISION-use-sqlite",)
    assert summary.facts == ("FACT-schema-v3",)
    retained_ids = {item.event_id for item in summary.modalities}
    assert {"req-1", "rationale-1", "attempt-1", "attempt-2", "model-failure"} <= retained_ids
    assert "chat-1" not in retained_ids  # plain chatter collapses into the range

    # 2. Bulky tool output left live context by reference, not by inlining.
    for token in BULKY_TOKENS:
        assert token not in text
    assert len(text.encode("utf-8")) < 4096
    references = _references(summary)
    assert set(references) == {"attempt-1", "bulky-ok"}
    assert references["attempt-1"]["exit_code"] == 2
    assert references["attempt-1"]["error"] == "FAILURE-attempt-1-linker"

    # 3. ...and remains recoverable, digest-verified, after restart.
    by_id = {event.event_id: event for event in source}
    for event_id, reference in references.items():
        assert service.retrieve_reference(SESSION, reference) == dict(by_id[event_id].payload)

    # 4. Critical history is recallable with provenance from the archive.
    recalled = service.recall_critical(SESSION, "compaction-1")
    assert [event.event_id for event in recalled] == [
        "req-1", "decision-1", "attempt-1", "attempt-2", "model-failure",
    ]
    assert all(event.event_hash == original[event.event_id] for event in recalled)
    assert [event.event_id for event in service.recover_source(SESSION, "compaction-1")] == [
        event.event_id for event in planted
    ]

    # 5. Compacted and post-compaction material stay searchable.
    compacted_hits = service.search_compacted(SESSION, "FAILURE-attempt-2")
    assert [(hit.event.event_id, hit.compaction_event_id) for hit in compacted_hits] == [
        ("attempt-2", "compaction-1"),
    ]
    live_hits = service.search_compacted(SESSION, "FAILURE-attempt-3")
    assert [(hit.event.event_id, hit.compaction_event_id) for hit in live_hits] == [
        ("attempt-3", None),
    ]
    assert service.search_compacted(SESSION, "LINKER-NOISE")[0].event.event_id == "attempt-1"

    # 6. Re-compaction from the original events reproduces the summary (COMPACT-004).
    again = SessionCompactionService(reopened, event_id_factory=lambda: "compaction-2").compact(
        SESSION, start_sequence=1, end_sequence=len(planted),
    )
    assert again.payload["summary"] == persisted[0].payload["summary"]
    assert again.payload["source_range"] == persisted[0].payload["source_range"]


class _LossyEngine(CompactionApplicationService):
    """A summarizer that drops failures and constraints but claims success."""

    def compact(self, request):
        result = super().compact(request)
        kept = tuple(
            item for item in result.summary.modalities
            if not item.event_type.endswith(".failed") and "constraints" not in item.payload
        )
        summary = CompactionSummary(
            facts=result.summary.facts, decisions=result.summary.decisions,
            modalities=kept,
        )
        return CompactionResult(
            result.session_id, result.source_range, summary,
            CompactionEvent(result.appended_event.event_id, result.session_id,
                            result.source_range, summary),
            CompactionValidation(True, detail="trust me"),
        )

    def validate(self, request, result):
        return CompactionValidation(True, detail="trust me")


def test_lossy_engine_cannot_append_a_summary_that_drops_failures(tmp_path):
    repo = SQLiteSessionRepository(tmp_path / "sessions.db")
    planted = _plant(repo)

    with pytest.raises(SessionCompactionError, match="omits critical history") as caught:
        SessionCompactionService(repo, engine=_LossyEngine()).compact(
            SESSION, start_sequence=1, end_sequence=len(planted),
        )
    assert "attempt-2: failure event omitted" in str(caught.value)
    assert "req-1: constraint event omitted" in str(caught.value)
    assert len(repo.read_range(SESSION, limit=100)) == len(planted)


def test_deterministic_engine_validation_reports_missing_critical_history(tmp_path):
    repo = SQLiteSessionRepository(tmp_path / "sessions.db")
    planted = _plant(repo)
    history = tuple(SessionCompactionService._history_event(event) for event in planted)
    source = SourceRange(SESSION, 1, len(planted), planted[0].event_id, planted[-1].event_id)
    request = CompactionRequest(SESSION, history, source)
    engine = CompactionApplicationService(event_id_factory=lambda: "c")
    full = engine.compact(request)
    assert full.validation.valid

    lossy = CompactionSummary(facts=full.summary.facts, decisions=full.summary.decisions)
    candidate = CompactionResult(
        SESSION, source, lossy, CompactionEvent("c", SESSION, source, lossy),
        CompactionValidation(True),
    )
    verdict = engine.validate(request, candidate)
    assert not verdict.valid
    assert "critical history missing" in verdict.detail


def _append_persisted(repo, events, summary, *, event_id, schema=None):
    payload = {
        "source_range": {
            "session_id": SESSION,
            "start_sequence": events[0].sequence,
            "end_sequence": events[-1].sequence,
            "start_event_id": events[0].event_id,
            "end_event_id": events[-1].event_id,
        },
        "summary": {
            "facts": list(summary.facts), "decisions": list(summary.decisions),
            "unresolved_tasks": list(summary.unresolved_tasks),
            "artifacts": list(summary.artifacts),
            "tool_outcomes": list(summary.tool_outcomes),
            "confidence": summary.confidence,
            "modalities": [
                {"event_id": item.event_id, "event_type": item.event_type,
                 "modality": item.modality, "payload": _json_value(item.payload)}
                for item in summary.modalities
            ],
        },
    }
    if schema is not None:
        payload["summary_schema"] = schema
    return repo.append(SESSION, "compaction.completed", payload, event_id=event_id)


def _request(events):
    return CompactionRequest(
        SESSION,
        tuple(SessionCompactionService._history_event(event) for event in events),
        SourceRange(SESSION, events[0].sequence, events[-1].sequence,
                    events[0].event_id, events[-1].event_id),
    )


def test_legacy_summary_that_collapsed_a_constraint_replays_lossless_view(tmp_path):
    repo = SQLiteSessionRepository(tmp_path / "sessions.db")
    planted = _plant(repo)
    legacy = canonical_summary(_request(planted), schema=1)
    assert "req-1" not in {item.event_id for item in legacy.modalities}
    event = _append_persisted(repo, planted, legacy, event_id="legacy-lossy")

    # Authentic legacy marker: replay re-derives the schema-2 view from source.
    summary = SessionCompactionService(repo).validate_persisted_event(event, planted)
    assert "req-1" in {item.event_id for item in summary.modalities}
    assert summary == canonical_summary(_request(planted))


def test_tampered_legacy_summary_still_fails_closed(tmp_path):
    repo = SQLiteSessionRepository(tmp_path / "sessions.db")
    planted = _plant(repo)
    legacy = canonical_summary(_request(planted), schema=1)
    forged = CompactionSummary(
        facts=legacy.facts, decisions=("FORGED",), modalities=legacy.modalities,
    )
    event = _append_persisted(repo, planted, forged, event_id="legacy-forged")

    with pytest.raises(SessionCompactionError, match="differs from canonical"):
        SessionCompactionService(repo).validate_persisted_event(event, planted)


def test_legacy_summary_without_critical_loss_still_replays(tmp_path):
    repo = SQLiteSessionRepository(tmp_path / "sessions.db")
    events = [
        repo.append(SESSION, "message.sent", {"text": "hi", "decisions": ["keep"]},
                    event_id="d"),
        repo.append(SESSION, "tool.failed", {"call_id": "x", "error": "boom"}, event_id="f"),
        repo.append(SESSION, "tool.result", {"call_id": "y", "content": "z" * 5000},
                    event_id="big"),
    ]
    legacy = canonical_summary(_request(events), schema=1)
    event = _append_persisted(repo, events, legacy, event_id="legacy-ok")

    summary = SessionCompactionService(repo).validate_persisted_event(event, events)
    assert summary.decisions == ("keep",)


def test_unknown_summary_schema_and_tampered_reference_fail_closed(tmp_path):
    repo = SQLiteSessionRepository(tmp_path / "sessions.db")
    planted = _plant(repo)
    current = canonical_summary(_request(planted))
    event = _append_persisted(repo, planted, current, event_id="future", schema=3)
    service = SessionCompactionService(repo)
    with pytest.raises(SessionCompactionError, match="schema"):
        service.validate_persisted_event(event, planted)

    reference = dict(_references(current)["bulky-ok"])
    assert service.retrieve_reference(SESSION, reference)["call_id"] == "a3"
    reference["reference_sha256"] = "0" * 64
    with pytest.raises(SessionCompactionError, match="digest"):
        service.retrieve_reference(SESSION, reference)
    with pytest.raises(SessionCompactionError, match="malformed"):
        service.retrieve_reference(SESSION, {"reference_sequence": 1})


class _Model:
    def __init__(self):
        self.requests = []

    def generate(self, request, context):
        self.requests.append(request)
        return ModelResponse("continuing", "fake", request.tier, tokens_out=1)


def test_live_lane_continues_after_compaction_and_restart_without_losing_history(tmp_path):
    """End-to-end: provider request after compaction + restart keeps critical history."""
    (tmp_path / "child").mkdir()
    sessions_path, fleet_path = tmp_path / "sessions.db", tmp_path / "fleet.db"
    sessions = SQLiteSessionRepository(sessions_path)
    store = SQLiteAgentLaneStore(fleet_path, sessions)
    service = AgentLaneService(store, sessions, _Model(), auto_start=False)
    context = local_owner_context(correlation_id="canary", workspace_roots=(tmp_path,))
    lane_id = service.spawn(
        command_id="canary-spawn", parent_session_id="parent", task="export data",
        workspace_root=str(tmp_path / "child"), context=context,
    )["lane"]["id"]
    session_id = store.read_lane(lane_id)["session_id"]

    sessions.append(session_id, "message.received", {
        "text": "requirements", "constraints": ["CONSTRAINT-no-network"],
    }, event_id="lane-req")
    sessions.append(session_id, "model.response", {
        "content": "DECISION-use-sqlite after FAILURE-attempt-1-linker",
    }, event_id="lane-decision")
    sessions.append(session_id, "tool.failed", {
        "call_id": "lane-a2", "error": "FAILURE-attempt-2-timeout",
    }, event_id="lane-failure")
    sessions.append(session_id, "tool.result", {
        "call_id": "lane-a3", "content": "BULKY-OK " * 3000,
    }, event_id="lane-bulky")
    events = sessions.read_range(session_id, limit=100)
    compacted = service._compaction.compact(
        session_id, start_sequence=events[0].sequence, end_sequence=events[-1].sequence,
    )
    assert compacted.payload["summary_schema"] == 2

    # Restart every durable component, then continue the lane.
    assert sessions.close() is True
    sessions = SQLiteSessionRepository(sessions_path)
    store = SQLiteAgentLaneStore(fleet_path, sessions)
    model = _Model()
    restarted = AgentLaneService(store, sessions, model, auto_start=False)
    restarted.run_pending(lane_id, context)

    assert store.read_lane(lane_id)["status"] == "completed"
    assert len(model.requests) == 1
    history = "\n".join(str(item["content"]) for item in model.requests[0].history)
    for token in (
        "CONSTRAINT-no-network", "DECISION-use-sqlite",
        "FAILURE-attempt-1-linker", "FAILURE-attempt-2-timeout",
    ):
        assert token in history, f"{token} lost from the continued provider request"
    assert "BULKY-OK" not in history
    assert "reference_sha256" in history

    summary = restarted._compaction.validate_persisted_event(
        compacted, sessions.read_range(
            session_id, start_sequence=events[0].sequence,
            end_sequence=events[-1].sequence, limit=len(events),
        ),
    )
    reference = _references(summary)["lane-bulky"]
    recovered = restarted._compaction.retrieve_reference(session_id, reference)
    assert recovered["content"].startswith("BULKY-OK")
