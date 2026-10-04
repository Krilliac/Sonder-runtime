"""Original #510: ordinary prose and summary references survive continuation."""
from __future__ import annotations

import json

import pytest

from sonder_runtime.adapters.persistence.agent_lanes import SQLiteAgentLaneStore
from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository
from sonder_runtime.application.agents.interactive_lanes import AgentLaneService
from sonder_runtime.application.compaction import SessionCompactionError, SessionCompactionService
from sonder_runtime.application.compaction.legacy import canonical_summary
from sonder_runtime.application.compaction.session_service import _json_value
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.compaction import CompactionRequest, SourceRange
from sonder_runtime.domain.context.compaction import compact_messages, COMPACTION_NOTE
from sonder_runtime.interfaces.agent_lanes import dispatch_agent_lane_tool
from tests.test_compaction_continue_canary import _Model


def test_overflow_preserves_unstructured_constraints_decisions_and_failures():
    earlier = [
        {"role": "user", "content": "Keep all export data offline. Do not use the network."},
        {"role": "assistant", "content": "Accepted: use sqlite, because the JSON attempt failed."},
        {"role": "user", "content": "The first build failed with a linker error."},
        {"role": "assistant", "content": "Keep that failed attempt in the history; use a different build command."},
    ]
    messages = [{"role": "system", "content": "Help with the exporter."}, *earlier]
    messages.extend({"role": role, "content": "continue"} for role in ("user", "assistant") * 2)
    messages.append({"role": "user", "content": "Now finish the exporter."})
    snapshot = json.dumps(messages)
    compacted = compact_messages(messages)
    assert compacted is not None
    assert compacted[1]["content"].startswith(COMPACTION_NOTE + "\n")
    assert json.loads(compacted[1]["content"].split("\n", 1)[1]) == [
        [message["role"], message["content"]] for message in earlier
    ]
    assert compacted[-1] == messages[-1]
    assert len(json.dumps(compacted).encode()) < len(snapshot.encode())
    assert json.dumps(messages) == snapshot


def test_overflow_does_not_discard_tool_evidence_or_oversized_old_prose():
    for earlier in (
        [{"role": "assistant", "content": "run build", "tool_calls": [{"id": "build-1"}]},
         {"role": "tool", "content": "build failed", "tool_call_id": "build-1"}],
        [{"role": "user", "content": "offline " * 40_000},
         {"role": "assistant", "content": "accepted"}],
    ):
        messages = [*earlier, {"role": "user", "content": "newer"},
                    {"role": "assistant", "content": "response"},
                    {"role": "user", "content": "finish"}]
        before = json.dumps(messages)
        assert compact_messages(messages) is None
        assert json.dumps(messages) == before


def test_authentic_schema_2_summary_replays_unstructured_prose_from_original_events(tmp_path):
    repository = SQLiteSessionRepository(tmp_path / "sessions.db")
    sources = [
        repository.append("s", "message.received", {"text": "Keep the exporter offline."}, event_id="constraint"),
        repository.append("s", "message.emitted", {"text": "Use sqlite because the first attempt failed."}, event_id="decision"),
    ]
    request = CompactionRequest(
        "s", tuple(SessionCompactionService._history_event(event) for event in sources),
        SourceRange("s", 1, 2, "constraint", "decision"),
    )
    old = canonical_summary(request, schema=2)
    assert old.modalities == ()
    event = repository.append("s", "compaction.completed", {
        "summary_schema": 2,
        "source_range": {"session_id": "s", "start_sequence": 1, "end_sequence": 2,
                         "start_event_id": "constraint", "end_event_id": "decision"},
        "summary": {"facts": [], "decisions": [], "unresolved_tasks": [], "artifacts": [],
                    "tool_outcomes": [], "confidence": None, "modalities": []},
    }, event_id="legacy-summary")
    summary = SessionCompactionService(repository).validate_persisted_event(event, sources)
    assert [dict(item.payload) for item in summary.modalities] == [dict(source.payload) for source in sources]
    assert repository.read_range("s", start_sequence=3, limit=1)[0] == event


@pytest.mark.parametrize("field", ["text", "content"])
def test_retained_prose_refuses_an_insufficient_summary_budget_without_truncating(tmp_path, field):
    repository = SQLiteSessionRepository(tmp_path / "sessions.db")
    source = repository.append("s", "message.received", {field: "Keep all exports offline."})
    service = SessionCompactionService(repository)
    with pytest.raises(SessionCompactionError, match="max_summary_tokens"):
        service.compact("s", start_sequence=1, end_sequence=1, max_summary_tokens=2)
    assert repository.read_range("s", limit=10) == (source,)
    summary = service.compact("s", start_sequence=1, end_sequence=1, max_summary_tokens=4)
    assert summary.payload["summary"]["modalities"][0]["payload"][field] == source.payload[field]


def test_lane_restart_preserves_prose_nested_summary_and_recovers_reference_via_tool(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    database = tmp_path / "sessions.db"
    sessions = SQLiteSessionRepository(database)
    store = SQLiteAgentLaneStore(tmp_path / "fleet.db", sessions)
    service = AgentLaneService(store, sessions, _Model(), auto_start=False)
    context = local_owner_context(correlation_id="continue", workspace_roots=(tmp_path,))
    lane_id = service.spawn(
        command_id="spawn", parent_session_id="parent", task="export data",
        workspace_root=str(workspace), context=context,
    )["lane"]["id"]
    session_id = store.read_lane(lane_id)["session_id"]
    sessions.append(session_id, "message.received", {"text": "Keep the exporter offline."}, event_id="plain-constraint")
    sessions.append(session_id, "message.emitted", {"text": "Accepted sqlite after the failed JSON attempt."}, event_id="plain-decision")
    sessions.append(session_id, "tool.failed", {
        "call_id": "build-1", "error": "linker failure", "receipt": {"status": "failed", "exit_code": 2},
    }, event_id="nested-failure")
    bulky = {"call_id": "build-2", "content": "compiler detail " * 3000,
             "result": {"files": ["app.py"], "status": "ok"}}
    sessions.append(session_id, "tool.result", bulky, event_id="bulky-output")
    sources = sessions.read_range(session_id, limit=100)
    service._compaction.compact(session_id, start_sequence=sources[0].sequence, end_sequence=sources[-1].sequence)
    sessions.close()
    sessions = SQLiteSessionRepository(database)
    store = SQLiteAgentLaneStore(tmp_path / "fleet.db", sessions)
    model = _Model()
    restarted = AgentLaneService(store, sessions, model, auto_start=False)
    restarted.run_pending(lane_id, context)
    assert store.read_lane(lane_id)["status"] == "completed"
    assert len(model.requests) == 1
    history = "\n".join(item["content"] for item in model.requests[0].history)
    for phrase in ("Keep the exporter offline.", "Accepted sqlite", "failed JSON attempt", "linker failure", '"exit_code": 2'):
        assert phrase in history
    assert "compiler detail" not in history
    recovered = dispatch_agent_lane_tool(
        restarted, "retrieve_archive", {"lane_id": lane_id, "archive_id": "bulky-output"},
        context, "parent", bound_parent_session_id="parent",
    )
    assert recovered["payload"] == bulky
    assert json.loads(json.dumps(recovered))["payload"]["result"]["files"] == ["app.py"]
    source = sessions.read_range(session_id, start_sequence=sources[-1].sequence, limit=1)[0]
    assert _json_value(source.payload) == bulky
