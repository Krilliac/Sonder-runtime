from __future__ import annotations

import pytest

import intents
from sonder_runtime.application.chat.handle_chat import ChatCommand, ChatService
from sonder_runtime.application.chat.lanes import (
    ChatHandoffProvenance,
    ChatLaneService,
)
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelResponse
from sonder_runtime.domain.routing.route_planner import RoutePlanner, RoutingRequest
from sonder_runtime.domain.runtime_policy import rules


class _Gateway:
    def __init__(self) -> None:
        self.request = None

    def generate(self, request, context):
        self.request = request
        return ModelResponse("hello", "fixture", request.tier)


def _provenance() -> ChatHandoffProvenance:
    return ChatHandoffProvenance(
        surface="test", reason="explicit bounded work", correlation_id="corr-1",
    )


def test_ordinary_conversation_stays_in_chat_without_a_handoff():
    decision = ChatLaneService(intents.classify_execution).decide(
        "How does a compiler work?", provenance=_provenance(),
    )

    assert decision.lane == "chat"
    assert decision.reason == "ordinary conversation"
    assert decision.handoff is None


def test_explicit_work_handoff_preserves_exact_objective_and_bounded_context():
    objective = "  Build the Flutter app.  "
    decision = ChatLaneService(intents.classify_execution).decide(
        objective,
        project="demo",
        constraints=("developer authorization required",),
        durable_context_refs=("session:s-1", "project:demo"),
        success_criteria=("return the lane receipt",),
        provenance=_provenance(),
    )

    assert decision.lane == "workbench"
    assert decision.handoff is not None
    assert decision.handoff.objective == objective
    assert decision.handoff.requested_mode == "workbench"
    assert decision.handoff.project == "demo"
    assert decision.handoff.as_dict()["durable_context_refs"] == [
        "session:s-1", "project:demo",
    ]


def test_handoff_rejects_unbounded_durable_context_refs():
    with pytest.raises(ValueError, match="durable_context_refs exceeds"):
        ChatLaneService(intents.classify_execution).decide(
            "Build the Flutter app.",
            durable_context_refs=tuple(f"ref-{value}" for value in range(9)),
            provenance=_provenance(),
        )


def test_chat_service_uses_policy_route_and_durable_lane_metadata():
    gateway = _Gateway()
    result = ChatService(gateway).complete(
        ChatCommand(content="hello"),
        local_owner_context(correlation_id="chat-lane", source="system"),
    )

    assert gateway.request.tier == "sonder"
    assert gateway.request.routing_metadata == {
        "lane": "chat", "reason": "ordinary conversation",
    }
    assert result.lane == "chat"
    assert result.routing_reason == "ordinary conversation"


def test_chat_service_refuses_execution_lanes_at_the_model_boundary():
    with pytest.raises(ValueError, match="only the chat routing lane"):
        ChatService(_Gateway()).complete(
            ChatCommand(content="run it", lane="workbench"),
            local_owner_context(correlation_id="chat-lane-refusal", source="system"),
        )


def test_runtime_policy_has_a_chat_lane_with_legacy_policy_fallback():
    policy = rules.default_policy({})
    assert policy["routing"]["chat"] == "general"

    legacy = rules.default_policy({})
    legacy["routing"].pop("chat")
    normalized = rules.normalize(legacy)
    assert normalized["routing"]["chat"] == "general"


def test_route_planner_keeps_ordinary_chat_on_the_policy_general_lane():
    policy = rules.default_policy({})
    route = RoutePlanner().select(
        RoutingRequest(lane="chat", prompt="hello there"),
        policy,
        RoutePlanner.from_policy(policy),
    )

    assert route.lane == "chat"
    assert route.tier == "general"
    assert route.model == policy["local_models"]["general"]


def test_chat_lane_metadata_is_persisted_with_the_model_request(tmp_path):
    from sonder_runtime.adapters.persistence.session_repository import (
        SQLiteSessionRepository,
    )
    from sonder_runtime.application.session.capture import SessionCaptureService
    from sonder_runtime.domain.common.ids import SessionId

    repository = SQLiteSessionRepository(tmp_path / "sessions.db", max_read_limit=100)
    session_id = SessionId.new()
    ChatService(_Gateway(), SessionCaptureService(repository)).complete(
        ChatCommand(content="durable route", session_id=session_id),
        local_owner_context(correlation_id="chat-lane-durable", source="system"),
    )

    event = repository.read_range(session_id.serialize(), limit=100)[0]
    assert event.event_type == "model.requested"
    assert event.payload["routing_metadata"] == {
        "lane": "chat", "reason": "ordinary conversation",
    }
    replay = SessionCaptureService(repository).replay(session_id.serialize())
    assert replay.request.request.routing_metadata == {
        "lane": "chat", "reason": "ordinary conversation",
    }


def test_large_ordinary_chat_is_not_limited_by_execution_handoff_bounds():
    decision = ChatLaneService(lambda _text: None).decide(
        "x" * 12_001, provenance=_provenance(),
    )

    assert decision.lane == "chat"


def test_handoff_normalizes_collections_and_rejects_string_collections():
    from sonder_runtime.application.chat.lanes import ChatHandoff

    handoff = ChatHandoff(
        objective="Build it.", requested_mode="workbench", project="demo",
        constraints=["no network"], durable_context_refs=["session:s-1"],
        success_criteria=["tests pass"], provenance=_provenance(),
    )
    assert handoff.constraints == ("no network",)
    with pytest.raises(ValueError, match="constraints must be a bounded sequence"):
        ChatHandoff(
            objective="Build it.", requested_mode="workbench", project="demo",
            constraints="no network", provenance=_provenance(),
        )
