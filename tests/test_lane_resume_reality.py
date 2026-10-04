"""Interactive lane resume-reality barrier coverage."""
from __future__ import annotations

from pathlib import Path
import subprocess

from sonder_runtime.adapters.persistence.agent_lanes import SQLiteAgentLaneStore
from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository
from sonder_runtime.application.agents.interactive_lanes import AgentLaneService
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelResponse
from sonder_runtime.adapters.workspace_reality import GitWorkspaceReality
from sonder_runtime.application.ports.tool_execution import ToolExecutionResult
from sonder_runtime.application.ports.tool_registry import InMemoryToolRegistry, ToolDescriptor
from sonder_runtime.application.tools.facade import ToolApplicationFacade
from sonder_runtime.application.tools.resource_policy import Decision, PolicyRule, ResourcePolicy
from sonder_runtime.domain.tools.descriptors import ToolEffect


class _Model:
    def __init__(self):
        self.requests = []

    def generate(self, request, context):
        self.requests.append(request)
        return ModelResponse("Plan: continue after inspection", "fake", request.tier, tokens_out=1)


class _Reality:
    def __init__(self):
        self.captures = 0
        self.revalidations = 0
        self.changed = False

    def capture(self, root):
        self.captures += 1
        return {"version": 1, "root": str(Path(root).resolve()), "head": "h", "dirty_hash": "d", "complete": True}

    def revalidate(self, root, snapshot, owned_paths=()):
        self.revalidations += 1
        if not self.changed:
            return None
        return {
            "status": "changed",
            "files": [{"path": "src.py", "status": "M"}],
            "requires_reinspection": True,
            "requires_replan": True,
            "snapshot": self.capture(root),
        }


def test_resume_reality_is_revalidated_and_delta_is_one_shot(tmp_path):
    sessions = SQLiteSessionRepository(tmp_path / "sessions.db")
    store = SQLiteAgentLaneStore(tmp_path / "lanes.db", sessions)
    model = _Model()
    reality = _Reality()
    service = AgentLaneService(store, sessions, model, auto_start=False, workspace_reality=reality)
    root = tmp_path / "child"
    root.mkdir()
    context = local_owner_context(correlation_id="test", workspace_roots=(tmp_path,))

    created = service.spawn(
        command_id="spawn",
        parent_session_id="parent",
        task="inspect",
        workspace_root=str(root),
        context=context,
    )
    lane_id = created["lane"]["id"]
    service.run_pending(lane_id, context)
    assert reality.captures == 1

    reality.changed = True
    service.control(lane_id, "resume", command_id="resume", context=context)
    service.run_pending(lane_id, context)
    assert reality.revalidations == 1
    assert '"src.py"' in model.requests[-1].prompt
    assert "resume reality delta" in model.requests[-1].prompt.casefold()

    # The durable projection remains private and the request-only delta is
    # consumed; a later request cannot repeat it from the same attempt.
    view = service.inspect(lane_id, context)["lane"]
    assert "resume_reality" not in view


def _git_repo(root: Path) -> Path:
    root.mkdir()
    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args], text=True)
    git("init", "-q")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    (root / "src.py").write_text("one\n", encoding="utf-8")
    git("add", "src.py")
    git("commit", "-qm", "initial")
    return root


def _tool_service(root: Path, replies, reality):
    sessions = SQLiteSessionRepository(root.parent / "sessions.db")
    store = SQLiteAgentLaneStore(root.parent / "lanes.db", sessions)
    model = _Model()
    writes = []

    class Executor:
        def execute(self, descriptor, call, ctx, execution_class):
            if descriptor.name == "write_file":
                (Path(ctx.workspace_roots[0]) / call.arguments["path"]).write_text(call.arguments["content"], encoding="utf-8")
                writes.append(call.arguments["path"])
            return ToolExecutionResult(tool_name=descriptor.name, success=True, output="ok")

    read = ToolDescriptor("file_read", input_schema={"type": "object"}, effects=frozenset({ToolEffect.READ_FILES}))
    write = ToolDescriptor(
        "write_file", input_schema={"type": "object"},
        effects=frozenset({ToolEffect.WRITE_FILES}),
    )
    service = AgentLaneService(
        store, sessions, model, auto_start=False, workspace_reality=reality,
    )
    service.tools = ToolApplicationFacade.compose(
        InMemoryToolRegistry([read, write]), Executor(),
        policy=ResourcePolicy([
            PolicyRule("allow", Decision.ALLOW, tool="file_read"),
            PolicyRule("allow", Decision.ALLOW, tool="write_file"),
        ]),
    )
    service.allowed_tools = frozenset({"file_read", "write_file"})
    model.generate = lambda request, ctx: (
        model.requests.append(request)
        or ModelResponse(next(replies), "fake", request.tier, tokens_out=1)
    )
    context = local_owner_context(correlation_id="test", workspace_roots=(root,))
    return service, model, context, writes


def test_real_git_changed_scope_blocks_mutation_before_tool_runner(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    reality = GitWorkspaceReality()
    service, model, context, writes = _tool_service(
        repo, iter(['done', '{"tool":"write_file","arguments":{"path":"new.txt","content":"x"}}']), reality,
    )
    created = service.spawn(command_id="spawn", parent_session_id="parent", task="inspect", workspace_root=str(repo), context=context)
    lane_id = created["lane"]["id"]
    service.run_pending(lane_id, context)
    (repo / "src.py").write_text("changed\n", encoding="utf-8")
    service.control(lane_id, "resume", command_id="resume", context=context)
    service.run_pending(lane_id, context)
    assert writes == []
    assert not (repo / "new.txt").exists()
    stable = lambda value: value.split("\nTool schema selection id:", 1)[0]
    assert stable(model.requests[-1].system) == stable(model.requests[0].system)
    assert "resume reality delta" not in model.requests[-1].system.casefold()
    service.control(lane_id, "resume", command_id="resume-again", context=context)
    service.run_pending(lane_id, context)
    assert writes == []
    assert "resume reality delta" in model.requests[-1].prompt.casefold()


def test_real_git_read_then_plan_clears_barrier_and_later_mutation_runs(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    reality = GitWorkspaceReality()
    service, model, context, writes = _tool_service(
        repo, iter(['done', '{"tool":"file_read","arguments":{"path":"src.py"}}', 'Plan: inspected', '{"tool":"write_file","arguments":{"path":"new.txt","content":"x"}}', 'done']), reality,
    )
    created = service.spawn(command_id="spawn", parent_session_id="parent", task="inspect", workspace_root=str(repo), context=context)
    lane_id = created["lane"]["id"]
    service.run_pending(lane_id, context)
    (repo / "src.py").write_text("changed\n", encoding="utf-8")
    service.control(lane_id, "resume", command_id="resume", context=context)
    service.run_pending(lane_id, context)
    assert writes == []
    stable = lambda value: value.split("\nTool schema selection id:", 1)[0]
    assert stable(model.requests[-1].system) == stable(model.requests[0].system)
    assert "resume reality delta" not in model.requests[-1].system.casefold()
    lane = service.store.read_lane(lane_id)
    assert lane["status"] == "completed"
    assert not lane["resume_reality"]["blocked"]
    assert sum("resume reality delta" in request.prompt.casefold() for request in model.requests) == 1
    service.control(lane_id, "resume", command_id="resume-after-plan", context=context)
    service.run_pending(lane_id, context)
    assert [Path(item) for item in writes] == [repo / "new.txt"]
    assert (repo / "new.txt").read_text(encoding="utf-8") == "x"


def test_real_git_identical_resume_has_no_delta_and_legacy_row_reinspects(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    service, model, context, _ = _tool_service(repo, iter(['done', 'done', 'done']), GitWorkspaceReality())
    lane_id = service.spawn(command_id="spawn", parent_session_id="parent", task="inspect",
                            workspace_root=str(repo), context=context)["lane"]["id"]
    service.run_pending(lane_id, context)
    service.control(lane_id, "resume", command_id="identical", context=context)
    service.run_pending(lane_id, context)
    assert "resume reality delta" not in model.requests[-1].prompt.casefold()
    with service.store.transaction() as tx:
        lane = tx.lane(lane_id)
        lane.pop("resume_reality", None)
        tx.save(lane)
    service.control(lane_id, "resume", command_id="legacy", context=context)
    service.run_pending(lane_id, context)
    assert "resume reality delta" in model.requests[-1].prompt.casefold()
    assert service.store.read_lane(lane_id)["resume_reality"]["blocked"]


def test_non_worktree_lane_resume_keeps_behavior(tmp_path):
    repo = tmp_path / "bare"
    repo.mkdir()
    subprocess.run(["git", "init", "--bare", str(repo)], check=True, capture_output=True)
    service, model, context, _ = _tool_service(repo, iter(['done', 'done']), GitWorkspaceReality())
    lane_id = service.spawn(command_id="spawn", parent_session_id="parent", task="inspect",
                            workspace_root=str(repo), context=context)["lane"]["id"]
    service.run_pending(lane_id, context)
    service.control(lane_id, "resume", command_id="resume", context=context)
    service.run_pending(lane_id, context)
    assert "resume reality delta" not in model.requests[-1].prompt.casefold()
    assert service.store.read_lane(lane_id)["status"] == "completed"


def test_resume_checks_git_at_dispatch_after_queue_delay(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    service, model, context, writes = _tool_service(repo, iter([
        'done', '{"tool":"write_file","arguments":{"path":"new.txt","content":"x"}}',
    ]), GitWorkspaceReality())
    lane_id = service.spawn(command_id="spawn", parent_session_id="parent", task="inspect",
                            workspace_root=str(repo), context=context)["lane"]["id"]
    service.run_pending(lane_id, context)
    service.control(lane_id, "resume", command_id="resume", context=context)
    (repo / "src.py").write_text("edited while queued", encoding="utf-8")
    service.run_pending(lane_id, context)
    assert writes == []
    assert "resume reality delta" in model.requests[-1].prompt.casefold()


def test_real_certificate_cannot_survive_resume_workspace_change(tmp_path):
    from tests.test_delegated_verification import _verifier, _prepared

    repo = _git_repo(tmp_path / "repo")
    service, model, context, _ = _tool_service(repo, iter(['done', 'done']), GitWorkspaceReality())
    parent = service.open_model_parent(context)
    lane_id = service.spawn(command_id="spawn", parent_session_id=parent["parent_session_id"], task="inspect",
                            workspace_root=str(repo), context=context)["lane"]["id"]
    service.run_pending(lane_id, context)
    lanes = (service, service.store, model, repo, context, parent)
    verifier, _, _ = _verifier(lanes)
    prepared = _prepared(lanes, verifier)
    result = verifier.execute_prepared(prepared, context=context, approve=lambda *_: "approved")
    assert result["state"] == "certified"
    assert verifier.validate(parent["parent_session_id"], prepared.verification_id,
                             context=context, bound_parent_revision=1).valid
    (repo / "src.py").write_text("external change", encoding="utf-8")
    service.control(lane_id, "resume", command_id="resume", context=context)
    service.run_pending(lane_id, context)
    verdict = verifier.validate(parent["parent_session_id"], prepared.verification_id,
                                context=context, bound_parent_revision=1)
    assert not verdict.valid and verdict.code == "STALE_OR_UNAVAILABLE"
