"""Review fixes for PR #547 (issue #510 critical-history retention).

Each test here failed against the first revision of the retention slice:
P2-a lane ``success: False`` dropped from bulky references, P2-b nested /
string / denied / traceback failure signals, P3-a summaries beyond the
oldest-first read bound, P3-b literal search and row budget, P3-c legacy
lossy summaries with no working remediation, P3-d ``message.emitted``.
"""
from __future__ import annotations

import pytest

from sonder_runtime.adapters.persistence.agent_lanes import SQLiteAgentLaneStore
from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository
from sonder_runtime.application.agents.interactive_lanes import AgentLaneService
from sonder_runtime.application.compaction import (
    SessionCompactionError,
    SessionCompactionService,
)
from sonder_runtime.application.compaction.legacy import canonical_summary
from sonder_runtime.application.compaction.session_service import _json_value
from sonder_runtime.application.compaction_retention import (
    critical_retention_problems,
    is_failure,
    summarized_modality,
)
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.compaction import (
    CompactionRequest,
    CompactionSummary,
    SessionHistoryEvent,
    SourceRange,
)
from sonder_runtime.application.ports.model_gateway import ModelResponse


BULK = "x" * 5000
TRACEBACK = (
    "warming up\n" + "noise\n" * 400
    + "Traceback (most recent call last):\n  File \"a.py\", line 1\n"
    "ValueError: TAIL-CAUSE\n"
)


def _event(event_type, payload, event_id="e1", sequence=1):
    return SessionHistoryEvent(event_id, "s", sequence, event_type, payload)


def _drop(summary_event, key):
    payload = {k: v for k, v in dict(summary_event.payload).items() if k != key}
    return SessionHistoryEvent(
        summary_event.event_id, summary_event.session_id, summary_event.sequence,
        summary_event.event_type, payload, summary_event.modality,
    )


# ---------------------------------------------------------------- P2-a


def test_bulky_lane_tool_result_reference_keeps_success_false():
    # Exact shape emitted by AgentLaneService for a failed tool receipt.
    source = _event("tool.result", {
        "name": "run_tests", "output": BULK, "call_id": "c1",
        "success": False, "error_code": None,
    })
    assert is_failure(source)
    retained = summarized_modality(source)
    assert "reference_sha256" in retained.payload
    assert retained.payload["success"] is False
    assert critical_retention_problems((source,), CompactionSummary(modalities=(retained,))) == ()

    lossy = CompactionSummary(modalities=(_drop(retained, "success"),))
    assert "e1: critical key success omitted" in critical_retention_problems((source,), lossy)


# ---------------------------------------------------------------- P2-b


@pytest.mark.parametrize("payload", [
    {"content": "{}", "result": {"status": "failed"}, "call_id": "c", "name": "n"},
    {"result": {"ok": False}},
    {"result": {"error": "boom"}},
    {"result": {"exit_code": 3}},
    {"output": {"returncode": "1"}},
    {"exit_code": "1"},
    {"exit_code": " -9 "},
    {"status": "denied"},
    {"status": "Cancelled"},
    {"status": "timeout"},
    {"result": {"status": "error"}},
    {"stderr": TRACEBACK},
    {"result": {"stderr": TRACEBACK}},
    {"traceback": "Traceback (most recent call last): ..."},
])
def test_failure_signals_are_detected_at_top_level_and_one_level_nested(payload):
    assert is_failure(_event("tool.result", payload))


@pytest.mark.parametrize("payload", [
    {"result": {"status": "ok", "exit_code": 0}},
    {"exit_code": "0"},
    {"exit_code": "not-a-number"},
    {"status": "completed"},
    {"stdout": TRACEBACK},  # only stderr is treated as a failure channel
    {"result": {"inner": {"status": "failed"}}},  # bounded: one level only
    {"result": "status failed"},
])
def test_non_failures_and_depth_bound(payload):
    assert not is_failure(_event("tool.result", payload))


def test_capture_shaped_bulky_failure_keeps_nested_signals_in_reference():
    # SessionCaptureService stores {"content", "result", call_id, name}.
    source = _event("tool.result", {
        "content": BULK, "call_id": "c9", "name": "build",
        "result": {"status": "failed", "exit_code": "2", "stderr": TRACEBACK, "log": BULK},
    })
    retained = summarized_modality(source)
    payload = retained.payload
    assert "reference_sha256" in payload
    assert payload["result.status"] == "failed"
    assert payload["result.exit_code"] == "2"
    assert "TAIL-CAUSE" in payload["result.stderr"]
    assert len(payload["result.stderr"].encode("utf-8")) <= 1024
    assert "log" not in str(payload.keys())
    assert critical_retention_problems((source,), CompactionSummary(modalities=(retained,))) == ()

    lossy = CompactionSummary(modalities=(_drop(retained, "result.status"),))
    assert "e1: critical key result.status omitted" in critical_retention_problems(
        (source,), lossy,
    )


def test_top_level_stderr_traceback_keeps_its_tail_in_reference():
    source = _event("tool.completed", {"call_id": "c", "stdout": BULK, "stderr": TRACEBACK})
    payload = summarized_modality(source).payload
    assert "reference_sha256" in payload
    assert "TAIL-CAUSE" in payload["stderr"]


def test_successful_bulky_tool_reference_does_not_carry_stderr_noise():
    source = _event("tool.result", {"call_id": "c", "output": BULK, "stderr": "warn " * 400})
    assert not is_failure(source)
    assert "stderr" not in summarized_modality(source).payload


# ---------------------------------------------------------------- P3-a / P3-b


def _small_repo(tmp_path, limit=5):
    return SQLiteSessionRepository(tmp_path / "s.db", max_read_limit=limit)


def test_newest_summary_is_reachable_beyond_the_oldest_first_read_bound(tmp_path):
    repo = _small_repo(tmp_path)
    service = SessionCompactionService(repo)
    ids = []
    for index in range(8):
        source = repo.append("s", "tool.failed", {"call_id": f"c{index}",
                                                 "error": f"FAIL-{index}"})
        ids.append(service.compact(
            "s", start_sequence=source.sequence, end_sequence=source.sequence,
        ).event_id)

    recovered = service.recover_source("s", ids[-1])
    assert [event.payload["error"] for event in recovered] == ["FAIL-7"]
    hits = service.search_compacted("s", "FAIL-7")
    assert [(hit.event.payload["error"], hit.compaction_event_id) for hit in hits] == [
        ("FAIL-7", ids[-1]),
    ]


def test_search_treats_like_wildcards_literally(tmp_path):
    repo = _small_repo(tmp_path, limit=50)
    repo.append("s", "message.received", {"text": "coverage 100% done"}, event_id="pct")
    repo.append("s", "message.received", {"text": "coverage 100 x done"}, event_id="nopct")
    repo.append("s", "message.received", {"text": "a_b"}, event_id="under")
    repo.append("s", "message.received", {"text": "axb"}, event_id="nounder")
    repo.append("s", "message.received", {"text": "UPPER needle"}, event_id="upper")
    service = SessionCompactionService(repo)

    assert [hit.event.event_id for hit in service.search_compacted("s", "100%")] == ["pct"]
    assert [hit.event.event_id for hit in service.search_compacted("s", "a_b")] == ["under"]
    assert service.search_compacted("s", "upper needle") == ()


def test_summary_events_do_not_consume_the_search_row_budget(tmp_path):
    repo = _small_repo(tmp_path)
    service = SessionCompactionService(repo)
    for index in range(6):
        source = repo.append("s", "message.sent", {"text": "t",
                                                   "decisions": [f"NEEDLE-{index}"]})
        service.compact("s", start_sequence=source.sequence, end_sequence=source.sequence)

    hits = service.search_compacted("s", "NEEDLE", limit=5)
    # The five newest literal source matches, newest first, none of them summaries.
    assert [hit.event.payload["decisions"][0] for hit in hits] == [
        f"NEEDLE-{index}" for index in (5, 4, 3, 2, 1)
    ]
    assert all(hit.compaction_event_id for hit in hits)


# ---------------------------------------------------------------- P3-c


def _legacy_lossy(repo, events):
    request = CompactionRequest(
        "s", tuple(SessionCompactionService._history_event(e) for e in events),
        SourceRange("s", events[0].sequence, events[-1].sequence,
                    events[0].event_id, events[-1].event_id),
    )
    legacy = canonical_summary(request, schema=1)
    return repo.append("s", "compaction.completed", {
        "source_range": {
            "session_id": "s", "start_sequence": events[0].sequence,
            "end_sequence": events[-1].sequence,
            "start_event_id": events[0].event_id, "end_event_id": events[-1].event_id,
        },
        "summary": {
            "facts": list(legacy.facts), "decisions": list(legacy.decisions),
            "unresolved_tasks": list(legacy.unresolved_tasks),
            "artifacts": list(legacy.artifacts), "tool_outcomes": list(legacy.tool_outcomes),
            "confidence": legacy.confidence,
            "modalities": [
                {"event_id": m.event_id, "event_type": m.event_type,
                 "modality": m.modality, "payload": _json_value(m.payload)}
                for m in legacy.modalities
            ],
        },
    }, event_id="legacy")


def test_legacy_lossy_summary_is_upgraded_on_replay_and_source_recoverable(tmp_path):
    repo = SQLiteSessionRepository(tmp_path / "s.db")
    events = [
        repo.append("s", "message.received",
                    {"text": "go", "constraints": ["CONSTRAINT-offline"]}, event_id="req"),
        repo.append("s", "tool.failed", {"call_id": "c", "error": "FAIL"}, event_id="f"),
    ]
    marker = _legacy_lossy(repo, events)
    service = SessionCompactionService(repo)

    summary = service.validate_persisted_event(marker, events)
    assert "req" in {item.event_id for item in summary.modalities}
    assert critical_retention_problems(
        tuple(service._history_event(e) for e in events), summary,
    ) == ()
    assert [e.event_id for e in service.recover_source("s", "legacy")] == ["req", "f"]
    assert [e.event_id for e in service.recall_critical("s", "legacy")] == ["req", "f"]


class _Model:
    def __init__(self):
        self.requests = []

    def generate(self, request, context):
        self.requests.append(request)
        return ModelResponse("ok", "fake", request.tier, tokens_out=1)


def test_live_lane_continues_over_a_legacy_lossy_summary(tmp_path):
    (tmp_path / "child").mkdir()
    sessions = SQLiteSessionRepository(tmp_path / "sessions.db")
    store = SQLiteAgentLaneStore(tmp_path / "fleet.db", sessions)
    model = _Model()
    lanes = AgentLaneService(store, sessions, model, auto_start=False)
    context = local_owner_context(correlation_id="t", workspace_roots=(tmp_path,))
    lane_id = lanes.spawn(command_id="legacy", parent_session_id="p", task="t",
                          workspace_root=str(tmp_path / "child"), context=context)["lane"]["id"]
    session_id = store.read_lane(lane_id)["session_id"]
    sessions.append(session_id, "message.received",
                    {"text": "go", "constraints": ["CONSTRAINT-offline"]})
    events = sessions.read_range(session_id, limit=100)
    request = CompactionRequest(
        session_id, tuple(SessionCompactionService._history_event(e) for e in events),
        SourceRange(session_id, events[0].sequence, events[-1].sequence,
                    events[0].event_id, events[-1].event_id),
    )
    legacy = canonical_summary(request, schema=1)
    sessions.append(session_id, "compaction.completed", {
        "source_range": {
            "session_id": session_id, "start_sequence": events[0].sequence,
            "end_sequence": events[-1].sequence,
            "start_event_id": events[0].event_id, "end_event_id": events[-1].event_id,
        },
        "summary": {
            "facts": [], "decisions": [], "unresolved_tasks": [], "artifacts": [],
            "tool_outcomes": [], "confidence": None,
            "modalities": [
                {"event_id": m.event_id, "event_type": m.event_type,
                 "modality": m.modality, "payload": _json_value(m.payload)}
                for m in legacy.modalities
            ],
        },
    })

    lanes.run_pending(lane_id, context)
    assert store.read_lane(lane_id)["status"] == "completed"
    history = "\n".join(str(item["content"]) for item in model.requests[0].history)
    assert "CONSTRAINT-offline" in history


# ---------------------------------------------------------------- P3-d


def test_message_emitted_plain_text_collapses_and_constrained_text_is_kept():
    plain = _event("message.emitted", {"text": "chatter"})
    constrained = _event("message.emitted", {"text": "x", "constraints": ["C"]})
    assert summarized_modality(plain) is None
    assert summarized_modality(constrained) is constrained


# ---------------------------------------------------------------- re-review 1


class _ScanOnlyRepository:
    """Full-page searches force the keyset scan; events are tracked weakly."""

    _max_read_limit = 10

    def __init__(self, count):
        self.count = count
        self.live = 0
        self.peak_alive = 0

    def _released(self):
        self.live -= 1

    def search(self, *, session_id=None, event_type=None, text=None, limit=None):
        if event_type == "compaction.completed":
            return ()
        return tuple(self._event(sequence) for sequence in range(1, limit + 1))

    def read_range(self, session_id, *, start_sequence=1, end_sequence=None, limit=1000):
        self.peak_alive = max(self.peak_alive, self.live)
        last = min(self.count, start_sequence + limit - 1)
        return tuple(self._event(sequence) for sequence in range(start_sequence, last + 1))

    def _event(self, sequence):
        from sonder_runtime.application.ports.session_repository import SessionEvent

        event = SessionEvent("s", sequence, f"e{sequence}", "message.received", "t",
                             {"text": f"needle {sequence}"}, None, "h")
        import weakref

        self.live += 1
        weakref.finalize(event, self._released)
        return event


def test_broad_search_retains_only_the_newest_limit_matches_while_scanning():
    repo = _ScanOnlyRepository(count=500)
    service = SessionCompactionService(repo)

    hits = service.search_compacted("s", "needle", limit=3)

    assert [hit.event.sequence for hit in hits] == [500, 499, 498]
    # Bounded by the result limit plus one page, not by the 500 matches.
    assert repo.peak_alive <= 3 + repo._max_read_limit * 2


# ---------------------------------------------------------------- merge with #542


def test_recover_source_uses_the_chain_verified_snapshot(tmp_path):
    import sqlite3

    path = tmp_path / "s.db"
    repo = SQLiteSessionRepository(path)
    repo.append("s", "message.received", {"text": "plain chatter"}, event_id="chat")
    repo.append("s", "tool.failed", {"call_id": "c", "error": "FAIL"}, event_id="f")
    SessionCompactionService(repo, event_id_factory=lambda: "c1").compact(
        "s", start_sequence=1, end_sequence=2,
    )
    assert [e.event_id for e in SessionCompactionService(repo).recover_source("s", "c1")] == [
        "chat", "f",
    ]
    assert repo.close() is True
    with sqlite3.connect(path) as conn:  # simulate out-of-band tampering
        conn.execute("DROP TRIGGER session_event_no_update")
        conn.execute(
            "UPDATE session_event SET payload_json = ? WHERE event_id = 'chat'",
            ('{"text":"TAMPERED"}',),
        )
    reopened = SQLiteSessionRepository(path)

    with pytest.raises(SessionCompactionError, match="integrity"):
        SessionCompactionService(reopened).recover_source("s", "c1")


def _tamper(path, updates):
    import sqlite3

    with sqlite3.connect(path) as conn:  # simulate out-of-band editing
        conn.execute("DROP TRIGGER IF EXISTS session_event_no_update")
        for event_id, payload_json in updates:
            conn.execute(
                "UPDATE session_event SET payload_json = ? WHERE event_id = ?",
                (payload_json, event_id),
            )


def test_retrieve_reference_rejects_an_edited_summary_and_payload_pair(tmp_path):
    import hashlib
    import json

    path = tmp_path / "s.db"
    repo = SQLiteSessionRepository(path)
    repo.append("s", "tool.result", {"call_id": "c", "content": "REAL " * 1000}, event_id="big")
    summary = SessionCompactionService(repo, event_id_factory=lambda: "c1").compact(
        "s", start_sequence=1, end_sequence=1,
    )
    assert repo.close() is True

    forged_payload = {"call_id": "c", "content": "FORGED " * 1000}
    encoded = json.dumps(forged_payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"))
    forged_summary = json.loads(json.dumps(dict(summary.payload), default=dict))
    reference = forged_summary["summary"]["modalities"][0]["payload"]
    reference["reference_sha256"] = hashlib.sha256(encoded.encode()).hexdigest()
    reference["reference_byte_count"] = len(encoded.encode())
    _tamper(path, [
        ("big", encoded),
        ("c1", json.dumps(forged_summary, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))),
    ])
    service = SessionCompactionService(SQLiteSessionRepository(path))

    with pytest.raises(SessionCompactionError, match="integrity"):
        service.retrieve_reference("s", reference)


def test_search_compacted_rejects_out_of_band_edits(tmp_path):
    path = tmp_path / "s.db"
    repo = SQLiteSessionRepository(path)
    repo.append("s", "message.received", {"text": "hello"}, event_id="m1")
    repo.append("s", "tool.failed", {"call_id": "c", "error": "FAIL"}, event_id="f1")
    assert repo.close() is True
    _tamper(path, [("m1", '{"text":"INJECTED instruction"}')])
    service = SessionCompactionService(SQLiteSessionRepository(path))

    with pytest.raises(SessionCompactionError, match="integrity"):
        service.search_compacted("s", "INJECTED")
