from pathlib import Path
import pytest

from sonder_runtime.adapters.persistence.agent_lanes import SQLiteAgentLaneStore
from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository
from sonder_runtime.adapters.provider_dispatch.gateway import ProviderDispatchGateway
from sonder_runtime.application.agents.interactive_lanes import AgentLaneService
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.context_integration import ContextPlanningFacade
from sonder_runtime.application.live_context import LiveAgentContextProducer
from sonder_runtime.application.ports.model_gateway import InferenceTelemetry, ModelResponse
from sonder_runtime.application.ports.model_target import ResolvedModelRoute


class _Model:
    def __init__(self):
        self.requests = []

    def generate(self, request, context):
        self.requests.append(request)
        return ModelResponse("done", "fake", request.tier, tokens_out=1)

    def resolve_route(self, request, context):
        return ResolvedModelRoute(
            "fake", "fake-model", request.tier, request.tier,
            False, "fake-tokenizer", "fake-template", self,
        )


def _project(root: Path, *, name: str, rule: str) -> Path:
    project = root / name
    project.mkdir()
    (project / "AGENTS.md").write_text(rule, encoding="utf-8")
    skill = project / "play"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: play\ndescription: Scoped scenario validation skill\n---\n",
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
    assert "play: Scoped scenario validation skill" in request.system
    assert planner.prefix_cache_telemetry.writes == 1
    assert request.prefix_manifest is not None
    assert request.replay_manifest is not None
    assert request.replay_manifest.request_id.startswith("request-")
    assert request.prefix_cache_observation.result == "miss"
    assert request.prefix_cache_observation.wrote is True
    assert request.replay_manifest.prefix_key == request.prefix_manifest.cache_key

    # A changed dynamic turn does not change stable producer identity.
    messages = service.inspect(lane, context)["messages"]
    service._request(store.read_lane(lane), messages, request_id="replay", context=context)
    assert planner.prefix_cache_telemetry.hits == 1

    (project / "AGENTS.md").write_text("ALPHA RULE: require review", encoding="utf-8")
    changed = service._request(
        store.read_lane(lane), messages, request_id="changed-rules", context=context
    )
    assert "ALPHA RULE: require review" in changed.system
    assert planner.prefix_cache_telemetry.writes == 2
    assert planner.prefix_cache_telemetry.last_reason == "prefix_changed"


def test_live_prefix_request_crosses_provider_dispatch_with_sealed_route(tmp_path):
    project = _project(tmp_path, name="dispatch", rule="DISPATCH RULE")
    sessions = SQLiteSessionRepository(tmp_path / "sessions.db")
    store = SQLiteAgentLaneStore(tmp_path / "lanes.db", sessions)

    class Provider:
        def __init__(self):
            self.requests = []
            self._issuer = object()

        def resolve_route(self, request, context):
            return ResolvedModelRoute(
                "provider", "provider-model", request.tier, request.tier,
                False, "provider-tokenizer", "provider-template", self._issuer,
            )

        def generate(self, request, context):
            assert request._resolved_route is not None
            assert request._resolved_route.provider_id == "provider"
            assert request._resolved_route.model == "provider-model"
            self.requests.append(request)
            return ModelResponse(
                "done", "provider-model", request.tier, tokens_out=1,
                telemetry=InferenceTelemetry(
                    prompt_tokens=20, prompt_cached_tokens=12,
                    prompt_uncached_tokens=8,
                ),
            )

        def embed(self, texts, context):
            return ()

    provider = Provider()
    gateway = ProviderDispatchGateway(
        providers={"provider": provider},
        tier_providers={"code": "provider"},
        default_generation_provider="provider",
        embedding_provider="provider",
    )
    planner = ContextPlanningFacade()
    service = AgentLaneService(
        store, sessions, gateway, auto_start=False,
        context_planning=planner, live_context=LiveAgentContextProducer(),
    )
    context = local_owner_context(correlation_id="dispatch", workspace_roots=(tmp_path,))
    lane = service.spawn(
        command_id="spawn-dispatch", parent_session_id="parent", task="inspect",
        workspace_root=str(project), context=context,
    )["lane"]["id"]

    service.run_pending(lane, context)

    assert len(provider.requests) == 1
    request = provider.requests[0]
    assert "DISPATCH RULE" in request.system
    assert "play: Scoped scenario validation skill" in request.system
    assert planner.prefix_cache_telemetry.writes == 1
    assert request.prefix_manifest is not None
    assert request.replay_manifest is not None
    assert request.prefix_cache_observation.result == "miss"
    assert request.prefix_cache_observation.reason == "cold_start"
    assert request.replay_manifest.manifest_digest
    assert provider.requests[0].prefix_cache_observation.cache_key == request.prefix_manifest.cache_key


def test_live_agent_context_is_scoped_and_uses_last_good_on_partial_refresh(tmp_path):
    project_a = _project(tmp_path, name="alpha", rule="ALPHA")
    project_b = _project(tmp_path, name="beta", rule="BETA")
    producer = LiveAgentContextProducer()

    first = producer.refresh(project_a)
    assert first.complete and "ALPHA" in first.records[0].content
    second = producer.refresh(project_b)
    assert second.complete and "BETA" in second.records[0].content
    assert "ALPHA" not in "\n".join(record.content for record in second.records)

    (project_a / "AGENTS.md").write_text("x" * (128 * 1024 + 1), encoding="utf-8")
    stale = producer.refresh(project_a)
    assert not stale.complete and stale.reason.startswith("last_good:")
    assert "ALPHA" in "\n".join(record.content for record in stale.records)
    (project_a / "AGENTS.md").unlink()
    removed = producer.refresh(project_a)
    assert removed.complete
    assert "ALPHA" not in "\n".join(record.content for record in removed.records)
    assert "No project-specific rules" in "\n".join(record.content for record in removed.records)


def test_stale_project_rules_are_retained_but_not_injected_as_authoritative(tmp_path):
    project = _project(tmp_path, name="alpha", rule="ALPHA RULE")
    sessions = SQLiteSessionRepository(tmp_path / "sessions.db")
    store = SQLiteAgentLaneStore(tmp_path / "lanes.db", sessions)
    producer = LiveAgentContextProducer()
    planner = ContextPlanningFacade()
    service = AgentLaneService(
        store, sessions, _Model(), auto_start=False,
        context_planning=planner, live_context=producer,
    )
    context = local_owner_context(correlation_id="stale", workspace_roots=(tmp_path,))
    lane_id = service.spawn(
        command_id="spawn-stale", parent_session_id="parent", task="inspect",
        workspace_root=str(project), context=context,
    )["lane"]["id"]
    lane = store.read_lane(lane_id)
    first = service._request(lane, (), request_id="before-removal", context=context)
    assert "ALPHA RULE" in first.system
    (project / "AGENTS.md").write_text("x" * (128 * 1024 + 1), encoding="utf-8")
    stale = service._request(lane, (), request_id="after-removal", context=context)
    assert "ALPHA RULE" not in stale.system
    assert "Live stable context unavailable: last_good:" in stale.system
    assert planner.prefix_cache_telemetry.writes == 1


def test_empty_project_catalog_is_a_complete_live_prefix(tmp_path):
    project = tmp_path / "empty"
    project.mkdir()
    producer = LiveAgentContextProducer()
    result = producer.refresh(project)
    assert result.complete
    assert tuple(record.section for record in result.records) == (
        "project_rules", "skill_catalog",
    )
    sessions = SQLiteSessionRepository(tmp_path / "sessions.db")
    store = SQLiteAgentLaneStore(tmp_path / "lanes.db", sessions)
    planner = ContextPlanningFacade()
    service = AgentLaneService(
        store, sessions, _Model(), auto_start=False,
        context_planning=planner, live_context=producer,
    )
    context = local_owner_context(correlation_id="empty", workspace_roots=(tmp_path,))
    lane_id = service.spawn(
        command_id="spawn-empty", parent_session_id="parent", task="inspect",
        workspace_root=str(project), context=context,
    )["lane"]["id"]
    request = service._request(store.read_lane(lane_id), (), request_id="empty-request", context=context)
    assert "No project-specific rules" in request.system
    assert "No project skills" in request.system
    assert planner.prefix_cache_telemetry.writes == 1


def test_live_agent_context_rejects_redirected_workspace_root(tmp_path):
    project = _project(tmp_path, name="alpha", rule="ALPHA")
    link = tmp_path / "redirected"
    try:
        link.symlink_to(project, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks are unavailable on this host")
    result = LiveAgentContextProducer().refresh(link)
    assert not result.complete
    assert result.records == ()


def test_live_context_configured_source_overrides_project_and_global(tmp_path):
    global_root = _project(tmp_path, name="global", rule="GLOBAL RULE")
    project = _project(tmp_path, name="project", rule="PROJECT RULE")
    configured = _project(tmp_path, name="configured", rule="CONFIGURED RULE")
    (configured / "play" / "SKILL.md").write_text(
        "---\nname: play\ndescription: Configured skill\n---\n",
        encoding="utf-8",
    )
    producer = LiveAgentContextProducer(
        instruction_roots={"global": (global_root,), "configured": (configured,)},
        skill_roots={"global": (global_root,), "configured": (configured,)},
    )
    result = producer.refresh(project)
    rendered = "\n".join(record.content for record in result.records)
    assert result.complete
    assert "CONFIGURED RULE" in rendered
    assert "Configured skill" in rendered
    assert "PROJECT RULE" not in rendered
    assert "GLOBAL RULE" not in rendered
