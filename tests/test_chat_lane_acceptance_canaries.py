"""Acceptance canaries for the #510 chat-lane convergence delta.

These drive the production classifier, lane service, route policy, model
gateway and typed tool admission. They cover only the criteria that the
focused chat-lane suites did not already establish:

* test_chat_lane.py / test_chat_lane_server_wiring.py: single ordinary
  prompts stay chat, authorization gates HTTP work, explicit pins do not move
  and chat routing metadata is captured and replayed.
* test_intents.py / test_execution_routing.py: explicit autonomous and fleet
  asks select and dispatch Autopilot and Fleet.
"""
from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace

import pytest

import intents
import server
from sonder_runtime.adapters.inference.ollama_gateway import OllamaGateway
from sonder_runtime.adapters.model_bootstrap import LegacyModelBootstrapAdapter
from sonder_runtime.adapters.persistence.session_repository import (
    SQLiteSessionRepository,
)
from sonder_runtime.application.chat.handle_chat import ChatCommand, ChatService
from sonder_runtime.application.chat.lanes import (
    ChatHandoffProvenance,
    ChatLaneService,
)
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelRequest, ModelResponse
from sonder_runtime.application.ports.tool_execution import ToolExecutionResult
from sonder_runtime.application.ports.tool_registry import (
    InMemoryToolRegistry,
    ToolDescriptor,
    ToolSchemaSelection,
)
from sonder_runtime.application.tools.typed_gateway import PortBackedToolInvoker
from sonder_runtime.bootstrap import app as bootstrap_app
from sonder_runtime.domain.common.errors import Forbidden
from sonder_runtime.domain.mode_tool_policy import AgentMode, project_mode_tool_policy
from sonder_runtime.domain.routing.route_planner import RoutePlanner, RoutingRequest
from sonder_runtime.domain.runtime_policy import rules
from sonder_runtime.domain.tools.descriptors import ExecutionClass, ToolEffect
from sonder_runtime.interfaces.http import serve


def _provenance() -> ChatHandoffProvenance:
    return ChatHandoffProvenance(surface="canary", reason="acceptance canary")


def _lane(prompt: str) -> str:
    return ChatLaneService(intents.classify_execution).decide(
        prompt, provenance=_provenance(),
    ).lane


@pytest.mark.parametrize("prompt", [
    "Hello!",
    "Thanks, that fixed it!",
    "Can you explain how git merge works?",
    "What is the difference between a list and a tuple in Python?",
    "Tell me about the history of the web.",
    "Why does my test fail intermittently?",
    "Could you describe how the renderer works?",
    "Summarize this conversation",
    "Explain only how to fix the build",
    # A creative text form stays conversation even when its topic is a
    # workspace noun; these used to start a foreground workbench run.
    "Write a poem about a database",
    "Compose a short song about our server",
    "Please write me a funny story about the test suite",
])
def test_explanatory_conversational_and_content_prompts_stay_in_chat(prompt):
    assert _lane(prompt) == "chat"


@pytest.mark.parametrize(("prompt", "lane"), [
    ("Fix the failing test in tests/test_app.py", "workbench"),
    ("Refactor the parser module", "workbench"),
    # The content-form exemption never hides real workspace work.
    ("Write a poem to docs/poem.md", "workbench"),
    ("Write a poem about the database and then fix the parser module", "workbench"),
    ("Implement the login endpoint end-to-end without asking me", "autopilot"),
    ("Run a fleet of agents to audit the repository", "fleet"),
])
def test_workspace_asks_leave_chat_only_through_a_typed_handoff(prompt, lane):
    decision = ChatLaneService(intents.classify_execution).decide(
        prompt, project="demo", provenance=_provenance(),
    )

    assert decision.lane == lane
    assert decision.handoff is not None
    assert decision.handoff.requested_mode == lane
    assert decision.handoff.objective == prompt


class _RecordingExecutor:
    def __init__(self) -> None:
        self.calls = []

    def execute(self, descriptor, call, context, execution_class):
        self.calls.append(call.tool_name)
        return ToolExecutionResult(success=True, output="written")


class _AllowEveryEffect:
    """The most permissive authorization, so only lane visibility can refuse."""

    def authorize(self, descriptor, call, context) -> None:
        del descriptor, call, context

    def select_execution_class(self, descriptor):
        return descriptor.execution_class


def test_chat_cannot_execute_a_mutating_tool_merely_because_it_is_registered():
    registry = InMemoryToolRegistry((
        ToolDescriptor("read_file", effects=frozenset({ToolEffect.READ_FILES})),
        ToolDescriptor(
            "write_file", effects=frozenset({ToolEffect.WRITE_FILES}),
            execution_class=ExecutionClass.HOST,
        ),
    ))
    executor = _RecordingExecutor()
    owner = local_owner_context(correlation_id="chat-tool-canary", source="system")
    invoker = PortBackedToolInvoker(
        registry, _AllowEveryEffect(), executor, context_factory=lambda _request: owner,
    )
    tool_call_text = json.dumps({
        "tool_calls": [{"name": "write_file", "arguments": {"path": "a.py"}}],
    })
    requests = []

    class Gateway:
        def generate(self, request, context):
            requests.append(request)
            return ModelResponse(tool_call_text, "fixture", request.tier)

    result = ChatService(Gateway()).complete(ChatCommand(content="save it"), owner)

    # The chat request has no tool surface, and a tool-shaped answer is text.
    assert "tools" not in {field.name for field in dataclasses.fields(ModelRequest)}
    assert requests[0].options == {}
    assert result.response_text == tool_call_text
    assert result.lane == "chat"
    assert executor.calls == []

    # Projecting the whole executable inventory into the chat mode leaves no
    # visible tool; admission refuses before authorization or execution.
    inventory = [item.name for item in registry.executable_inventory().list_all()]
    chat_policy = project_mode_tool_policy(
        AgentMode.CHAT, inventory,
        read_only_tools=("read_file",), mutating_tools=("write_file",),
    )
    selection = ToolSchemaSelection(
        frozenset(chat_policy.available_tools()), selection_id="chat-turn",
    )
    assert chat_policy.available_tools() == ()
    for name in inventory:
        with pytest.raises(Forbidden, match="not visible"):
            invoker.invoke(SimpleNamespace(
                tool_name=name, arguments={"path": "a.py"}, request_id="call-1",
                schema_selection=selection,
            ))
    assert executor.calls == []


def test_one_model_bound_to_general_and_code_keeps_chat_and_work_lanes_distinct(
    monkeypatch,
):
    policy = rules.default_policy({})
    shared = policy["local_models"]["general"]
    assert policy["local_models"]["code"] == shared
    monkeypatch.setattr(server, "_refresh_live_cloud_tiers", lambda: None)
    monkeypatch.setattr(server, "_RUNTIME_POLICY", policy)
    monkeypatch.setattr(server, "_STRICT_DEFAULT", False)
    monkeypatch.setitem(server.TIERS, "general", shared)
    monkeypatch.setitem(server.TIERS, "code", shared)
    bootstrap = LegacyModelBootstrapAdapter(server)
    generated = []

    def make_generate(model, *_args, **_kwargs):
        generated.append(model)
        return lambda prompt, _history: "answer to " + prompt

    gateway = OllamaGateway(
        target_resolver=bootstrap.resolve_target, generate_factory=make_generate,
    )
    chat = ChatService(
        gateway, chat_default_tier=lambda: bootstrap.resolve_target("sonder").tier_label,
    )
    owner = local_owner_context(correlation_id="shared-model-canary", source="system")

    answer = chat.complete(ChatCommand(content="How does a compiler work?"), owner)
    pinned = chat.complete(ChatCommand(content="hello", tier="code"), owner)

    assert generated == [shared, shared]
    assert (answer.lane, answer.tier, answer.routing_reason) == (
        "chat", "general", "ordinary conversation",
    )
    assert pinned.tier == "code"
    planner = RoutePlanner()
    chat_route = planner.select(
        RoutingRequest(lane="chat", prompt="Fix the parser module"),
        policy, RoutePlanner.from_policy(policy),
    )
    assert (chat_route.lane, chat_route.tier, chat_route.model) == ("chat", "general", shared)
    # Classification, not the model binding, decides the lane.
    assert _lane("How does a compiler work?") == "chat"
    assert _lane("Fix the parser module") == "workbench"
    assert server.runtime_policy.route_tier("workbench", policy) == "code"
    assert server.runtime_policy.route_tier("chat", policy, fallback="general") == "general"


def test_admitted_work_receipt_records_the_lane_and_its_reason(tmp_path, monkeypatch):
    database = tmp_path / "canary-work.sqlite"
    repository = SQLiteSessionRepository(database)
    monkeypatch.setattr(
        bootstrap_app, "default_app",
        lambda: SimpleNamespace(session_repository=lambda: repository),
    )
    objective = "Implement the login endpoint end-to-end without asking me"
    dispatched = []

    def lane(prompt, *, project, _admitted_decision, **_kwargs):
        dispatched.append(_admitted_decision.lane)
        return "autopilot started"

    monkeypatch.setattr(server, "route_work_request", lane)
    result = serve._handle_work_intent(
        objective, project="demo", authorized=True,
        context={"mode": "local-open"}, session_id="canary-session",
        session_ref="canary-session", correlation_id="corr-canary",
        with_receipt=True,
    )

    assert dispatched == ["autopilot"]
    receipt = result.public_receipt()
    assert receipt["status"] == "returned"
    assert receipt["requested_mode"] == "autopilot"
    assert receipt["routing_reason"] == "explicit autonomous or end-to-end request"
    admitted = next(
        event for event in SQLiteSessionRepository(database).read_complete("canary-session")
        if event.event_type == "chat.work.admitted"
    )
    assert admitted.payload["requested_mode"] == "autopilot"
    assert admitted.payload["routing_reason"] == receipt["routing_reason"]
    assert "login endpoint" not in json.dumps(admitted.payload)
