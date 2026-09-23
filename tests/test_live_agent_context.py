from pathlib import Path

from sonder_runtime.adapters.persistence.agent_lanes import SQLiteAgentLaneStore
from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository
from sonder_runtime.application.agents.interactive_lanes import AgentLaneService
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.context_integration import ContextPlanningFacade
from sonder_runtime.application.live_context import LiveAgentContextProducer
from sonder_runtime.application.ports.model_gateway import ModelResponse


class _Model:
    def __init__(self):
        self.requests = []

    def generate(self, request, context):
        self.requests.append(request)
        return ModelResponse("done", "fake", request.tier, tokens_out=1)


def _project(root: Path, *, name: str, rule: str) -> Path:
    project = root / name
    project.mkdir()
    (project / "AGENTS.md").write_text(rule, encoding="utf-8")
    skill = project / "play"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: play\ndescription: Scoped playtest skill\n---\n",
        encoding="utf-8",
    )
    return project


def test_live_agent_request_assembles_scoped_rules_skills_and_reuses_prefix(tmp_path):
    project = _project(tmp_path, name="alpha", rule="ALPHA RULE: keep changes bounded")
    sessions = SQLiteSessionRepository(tmp_path / "sessions.db")
    store = SQLiteAgentLaneStore(tmp_path / "lanes.db", sessions)
    model = _Model()
    planner = ContextPlanningFacade()
    producer = LiveAgentContextProducer()
    service = AgentLaneService(
        store, sessions, model, auto_start=False,
        context_planning=planner, live_context=producer,
    )
    context = local_owner_context(correlation_id="test", workspace_roots=(tmp_path,))
    lane = service.spawn(
        command_id="spawn", parent_session_id="parent", task="inspect",
        workspace_root=str(project), context=context,
    )["lane"]["id"]

    service.run_pending(lane, context)
    request = model.requests[0]
    assert "ALPHA RULE: keep changes bounded" in request.system
    assert "play: Scoped playtest skill" in request.system
    assert planner.prefix_cache_telemetry.writes == 1

    # A changed dynamic turn does not change stable producer identity.
    messages = service.inspect(lane, context)["messages"]
    service._request(store.read_lane(lane), messages, request_id="replay")
    assert planner.prefix_cache_telemetry.hits == 1

    (project / "AGENTS.md").write_text("ALPHA RULE: require review", encoding="utf-8")
    changed = service._request(
        store.read_lane(lane), messages, request_id="changed-rules"
    )
    assert "ALPHA RULE: require review" in changed.system
    assert planner.prefix_cache_telemetry.writes == 2
    assert planner.prefix_cache_telemetry.last_reason == "prefix_changed"


def test_live_agent_context_is_scoped_and_uses_last_good_on_partial_refresh(tmp_path):
    project_a = _project(tmp_path, name="alpha", rule="ALPHA")
    project_b = _project(tmp_path, name="beta", rule="BETA")
    producer = LiveAgentContextProducer()

    first = producer.refresh(project_a)
    assert first.complete and "ALPHA" in first.records[0].content
    second = producer.refresh(project_b)
    assert second.complete and "BETA" in second.records[0].content
    assert "ALPHA" not in "\n".join(record.content for record in second.records)

    (project_a / "AGENTS.md").unlink()
    stale = producer.refresh(project_a)
    assert stale.complete and stale.reason.startswith("last_good:")
    assert "ALPHA" in "\n".join(record.content for record in stale.records)

