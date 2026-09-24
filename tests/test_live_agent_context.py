import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from sonder_runtime.adapters.persistence.agent_lanes import SQLiteAgentLaneStore
from sonder_runtime.adapters.persistence.session_repository import (
    SQLiteSessionRepository,
)
from sonder_runtime.adapters.provider_dispatch.gateway import ProviderDispatchGateway
from sonder_runtime.application.agents.interactive_lanes import AgentLaneService
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.context_integration import ContextPlanningFacade
from sonder_runtime.application.live_context import LiveAgentContextProducer
from sonder_runtime.application.ports.model_gateway import (
    InferenceTelemetry,
    ModelResponse,
)
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


class _RoutedProvider(_Model):
    """A deterministic route authority, without simulated provider cache hits."""

    def __init__(self, provider_id: str, *, artifact_digest: str):
        super().__init__()
        self.provider_id = provider_id
        self.artifact_digest = artifact_digest

    def resolve_route(self, request, context):
        return ResolvedModelRoute(
            self.provider_id, "model:latest", request.tier, request.tier,
            False, "tokenizer", (
                "ollama-template-sha256:" + "c" * 64
                + ";ollama-model-sha256:" + self.artifact_digest
                if self.artifact_digest else ""
            ), self,
        )

    def generate(self, request, context):
        assert request._resolved_route is not None
        assert request._resolved_route.provider_id == self.provider_id
        self.requests.append(request)
        return ModelResponse("done", "model:latest", request.tier, tokens_out=1)


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

    # A live skill catalog update is also stable prefix input.  It must be
    # observed through the real lane request path and carried into replay,
    # rather than being hidden behind the producer's previous snapshot.
    (project / "play" / "SKILL.md").write_text(
        "---\nname: play\ndescription: Updated scoped review skill\n---\n",
        encoding="utf-8",
    )
    changed_skill = service._request(
        store.read_lane(lane), messages, request_id="changed-skill", context=context
    )
    assert "play: Updated scoped review skill" in changed_skill.system
    assert changed_skill.prefix_cache_observation.result == "miss"
    assert changed_skill.prefix_cache_observation.reason == "prefix_changed"
    assert changed_skill.prefix_manifest.cache_key != changed.prefix_manifest.cache_key
    assert changed_skill.replay_manifest.prefix_key == changed_skill.prefix_manifest.cache_key
    assert changed_skill.replay_manifest.manifest_digest != changed.replay_manifest.manifest_digest
    assert planner.prefix_cache_telemetry.writes == 3


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


_SELECTION_MARKER = "\nTool schema selection id: "


def _tool_worker(worker_dir: Path, workspace_parent: Path):
    """One independent worker: its own stores, planner cache, and producer."""
    from sonder_runtime.application.ports.tool_registry import (
        InMemoryToolRegistry,
        ToolDescriptor,
    )
    from sonder_runtime.application.tools.facade import ToolApplicationFacade

    worker_dir.mkdir(parents=True, exist_ok=True)
    sessions = SQLiteSessionRepository(worker_dir / "sessions.db")
    store = SQLiteAgentLaneStore(worker_dir / "lanes.db", sessions)
    model = _Model()
    planner = ContextPlanningFacade()
    service = AgentLaneService(
        store, sessions, model, auto_start=False,
        context_planning=planner, live_context=LiveAgentContextProducer(),
    )
    service.tools = ToolApplicationFacade.compose(InMemoryToolRegistry([
        ToolDescriptor("read_file", input_schema={
            "type": "object", "properties": {"path": {"type": "string"}},
            "required": ["path"],
        }),
        ToolDescriptor("text_search", input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}, "path": {"type": "string"}},
            "required": ["query"],
        }),
        ToolDescriptor("directory_tree", input_schema={
            "type": "object", "properties": {"path": {"type": "string"}},
        }),
    ]))
    context = local_owner_context(
        correlation_id="worker", workspace_roots=(workspace_parent,),
    )
    return service, store, planner, context


def _spawn_request(service, store, context, project: Path, command: str, request_id: str):
    lane_id = service.spawn(
        command_id=command, parent_session_id="parent", task="inspect",
        workspace_root=str(project), context=context,
    )["lane"]["id"]
    lane = store.read_lane(lane_id)
    return lane, service._request(lane, (), request_id=request_id, context=context)


def _stable_system(request) -> str:
    """Text before the dynamic per-turn selection id, if it is the tail."""
    stable, marker, selection_id = request.system.rpartition(_SELECTION_MARKER)
    if not marker or "\n" in selection_id:
        return request.system
    return stable


def _worker_prefix_evidence(worker_dir: Path, project: Path) -> dict:
    service, store, _planner, context = _tool_worker(worker_dir, project.parent)
    _lane, request = _spawn_request(
        service, store, context, project, "spawn-worker", "worker-request",
    )
    return {
        "cache_key": request.prefix_manifest.cache_key,
        "identity_key": request.prefix_manifest.identity_key,
        "result": request.prefix_cache_observation.result,
        "reason": request.prefix_cache_observation.reason,
        "stable_system": _stable_system(request),
    }


def test_turn_selection_id_is_visible_but_outside_reusable_prefix(tmp_path):
    project = _project(tmp_path, name="alpha", rule="ALPHA RULE")
    service, store, planner, context = _tool_worker(tmp_path / "worker", tmp_path)
    lane, first = _spawn_request(service, store, context, project, "spawn-turns", "turn-1")
    next_turn = dict(lane, used_steps=lane["used_steps"] + 1)
    second = service._request(next_turn, (), request_id="turn-2", context=context)

    # The per-turn id is dynamic: the reusable key and stable bytes are equal.
    assert first.prefix_manifest.cache_key == second.prefix_manifest.cache_key
    assert second.prefix_cache_observation.result == "hit"
    first_id = first.system.rpartition(_SELECTION_MARKER)[2]
    second_id = second.system.rpartition(_SELECTION_MARKER)[2]
    assert first.system.endswith(_SELECTION_MARKER + first_id)
    assert first_id == lane["attempt_id"] + ":1"
    assert second_id == lane["attempt_id"] + ":2"
    assert "Visible tool schemas" in _stable_system(first)
    assert '"name": "text_search"' in _stable_system(first)
    assert "ALPHA RULE" in _stable_system(first)
    assert _stable_system(first) == _stable_system(second)
    assert first_id not in first.prefix_manifest.sections[0].content
    assert first.prefix_cache_observation.reason == "cold_start"
    assert planner.prefix_cache_telemetry.writes == 1
    assert planner.prefix_cache_telemetry.hits == 1
    assert first.replay_manifest.manifest_digest != second.replay_manifest.manifest_digest


def test_independent_workers_derive_identical_prefix_for_same_project(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    project = _project(shared, name="alpha", rule="ALPHA RULE")
    other = _project(shared, name="beta", rule="BETA RULE")
    one = _worker_prefix_evidence(tmp_path / "worker-1", project)
    two = _worker_prefix_evidence(tmp_path / "worker-2", project)
    beta = _worker_prefix_evidence(tmp_path / "worker-3", other)

    assert one["cache_key"] == two["cache_key"]
    assert one["identity_key"] == two["identity_key"]
    assert one["stable_system"] == two["stable_system"]
    # Each worker's application cache is process-local and says so honestly.
    assert (one["result"], one["reason"]) == ("miss", "cold_start")
    assert (two["result"], two["reason"]) == ("miss", "cold_start")
    # Cross-project scope never shares a reusable prefix.
    assert beta["cache_key"] != one["cache_key"]
    assert "ALPHA RULE" not in beta["stable_system"]


def test_live_route_replacement_and_origin_change_invalidate_prefix_identity(tmp_path):
    project = _project(tmp_path, name="alpha", rule="SCOPED RULE")
    sessions = SQLiteSessionRepository(tmp_path / "sessions.db")
    store = SQLiteAgentLaneStore(tmp_path / "lanes.db", sessions)
    provider = _RoutedProvider("origin-a", artifact_digest="a" * 64)
    gateway = ProviderDispatchGateway(
        providers={"origin-a": provider}, tier_providers={"code": "origin-a"},
        default_generation_provider="origin-a", embedding_provider="origin-a",
    )
    planner = ContextPlanningFacade()
    service = AgentLaneService(
        store, sessions, gateway, auto_start=False,
        context_planning=planner, live_context=LiveAgentContextProducer(),
    )
    context = local_owner_context(correlation_id="route", workspace_roots=(tmp_path,))
    lane_id = service.spawn(
        command_id="spawn-route", parent_session_id="parent", task="inspect",
        workspace_root=str(project), context=context,
    )["lane"]["id"]
    lane = store.read_lane(lane_id)
    first = service._request(lane, (), request_id="before-replace", context=context)
    gateway.generate(first, context)
    assert first.prefix_manifest is not None

    # A mutable tag can keep the same name and template while the provider's
    # resolved artifact changes. Its next live request must get a new prefix.
    provider.artifact_digest = "b" * 64
    replaced = service._request(lane, (), request_id="after-replace", context=context)
    gateway.generate(replaced, context)
    assert replaced.prefix_manifest.cache_key != first.prefix_manifest.cache_key
    assert replaced.replay_manifest.manifest_digest != first.replay_manifest.manifest_digest
    assert replaced.prefix_cache_observation.reason == "identity_changed"

    # Missing identity disables a positive cache claim even though ordinary
    # scoped rules remain visible and generation may still proceed.
    provider.artifact_digest = ""
    unknown = service._request(lane, (), request_id="unknown-model", context=context)
    gateway.generate(unknown, context)
    assert unknown.prefix_manifest is None
    assert unknown.prefix_cache_observation is None
    assert "SCOPED RULE" in unknown.system

    other = _RoutedProvider("origin-b", artifact_digest="b" * 64)
    other_gateway = ProviderDispatchGateway(
        providers={"origin-b": other}, tier_providers={"code": "origin-b"},
        default_generation_provider="origin-b", embedding_provider="origin-b",
    )
    other_service = AgentLaneService(
        store, sessions, other_gateway, auto_start=False,
        context_planning=ContextPlanningFacade(), live_context=LiveAgentContextProducer(),
    )
    other_request = other_service._request(
        lane, (), request_id="different-origin", context=context,
    )
    other_gateway.generate(other_request, context)
    assert other_request.prefix_manifest.cache_key != replaced.prefix_manifest.cache_key
    assert other_request.prefix_cache_observation.result == "miss"


def test_prefix_identity_is_stable_across_processes_and_hash_seeds(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    project = _project(shared, name="alpha", rule="ALPHA RULE")
    repo = Path(__file__).resolve().parents[1]
    observed = []
    for index, seed in enumerate(("1", "4242")):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        completed = subprocess.run(
            [sys.executable, "-m", "tests.test_live_agent_context",
             str(tmp_path / f"process-{index}"), str(project)],
            cwd=repo, env=env, capture_output=True, text=True, timeout=120,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr[-2000:]
        observed.append(json.loads(completed.stdout.strip().splitlines()[-1]))
    in_process = _worker_prefix_evidence(tmp_path / "in-process", project)
    assert observed[0]["cache_key"] == observed[1]["cache_key"] == in_process["cache_key"]
    assert observed[0]["stable_system_sha256"] == observed[1]["stable_system_sha256"]


def test_live_request_survives_restart_without_rewriting_stale_project_context(tmp_path):
    project = _project(tmp_path, name="alpha", rule="ORIGINAL RULE: keep edits scoped")
    worker_dir = tmp_path / "first-worker"
    service, store, planner, context = _tool_worker(worker_dir, tmp_path)
    lane_id = service.spawn(
        command_id="spawn-captured", parent_session_id="parent", task="inspect",
        workspace_root=str(project), context=context,
    )["lane"]["id"]

    # Execute the real lane admission, request builder, and durable outbox
    # before restarting the reader. This is the model-bound request, rather
    # than a test-constructed manifest passed directly to session capture.
    service.run_pending(lane_id, context)
    original = service.gateway.requests[0]
    session_id = store.read_lane(lane_id)["session_id"]
    session_path = worker_dir / "sessions.db"
    events = SQLiteSessionRepository(session_path).read_complete(session_id)
    snapshots = [event.payload for event in events if event.event_type == "model.requested"]
    assert len(snapshots) == 1
    assert any(event.event_type == "model.response" for event in events)
    persisted = snapshots[0]
    assert planner.prefix_cache_telemetry.writes == 1
    assert original.prefix_manifest is not None
    assert original.replay_manifest is not None
    assert "Visible tool schemas" in original.system
    assert "Tool schema selection id:" in original.system
    assert "Tool schema selection id:" not in original.prefix_manifest.sections[0].content
    assert persisted["prefix_manifest"]["cache_key"] == original.prefix_manifest.cache_key
    assert persisted["prefix_manifest"]["identity_key"] == original.prefix_manifest.identity_key
    assert persisted["replay_manifest"]["manifest_digest"] == original.replay_manifest.manifest_digest
    assert {section["section"] for section in persisted["replay_manifest"]["sections"]} == {
        "stable_instructions", "project_rules", "skill_catalog",
    }
    assert len(json.dumps(persisted["replay_manifest"]).encode("utf-8")) < 16_384
    assert "ORIGINAL RULE" not in json.dumps(persisted["replay_manifest"])
    assert "Scoped scenario validation skill" not in json.dumps(persisted["replay_manifest"])
    service.close()

    (project / "AGENTS.md").write_text("REVISED RULE: require a review", encoding="utf-8")
    (project / "play" / "SKILL.md").write_text(
        "---\nname: play\ndescription: Revised scoped skill\n---\n", encoding="utf-8",
    )
    completed = subprocess.run(
        [sys.executable, "-m", "tests.test_live_agent_context", "--replay",
         str(session_path), session_id, str(tmp_path / "restarted-worker"), str(project)],
        cwd=Path(__file__).resolve().parents[1],
        env=dict(os.environ, PYTHONHASHSEED="4242"),
        capture_output=True, text=True, timeout=120, check=False,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    restarted = json.loads(completed.stdout.strip().splitlines()[-1])
    assert restarted["crash_safe"] is True
    assert restarted["prefix"] == persisted["prefix_manifest"]
    assert restarted["replay"] == persisted["replay_manifest"]
    assert "ORIGINAL RULE" in restarted["system"]
    assert "REVISED RULE" not in restarted["system"]
    assert restarted["new_prefix"] != restarted["prefix"]["cache_key"]
    assert "REVISED RULE" in restarted["new_system"]
    assert "Revised scoped skill" in restarted["new_system"]
    assert "ORIGINAL RULE" not in restarted["new_system"]


if __name__ == "__main__":
    import hashlib

    if sys.argv[1] == "--replay":
        from sonder_runtime.application.session.durable_replay import crash_safe_replay

        recovered = crash_safe_replay(SQLiteSessionRepository(sys.argv[2]), sys.argv[3])
        assert recovered.request is not None
        request = recovered.request.request
        new = _worker_prefix_evidence(Path(sys.argv[4]), Path(sys.argv[5]))
        evidence = {
            "crash_safe": recovered.crash_safe,
            "prefix": request.prefix_manifest,
            "replay": request.replay_manifest,
            "system": request.system,
            "new_prefix": new["cache_key"],
            "new_system": new["stable_system"],
        }
    else:
        evidence = _worker_prefix_evidence(Path(sys.argv[1]), Path(sys.argv[2]))
        stable = evidence.pop("stable_system")
        evidence["stable_system_sha256"] = hashlib.sha256(stable.encode("utf-8")).hexdigest()
    print(json.dumps(evidence, sort_keys=True))
