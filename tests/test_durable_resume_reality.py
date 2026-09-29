from __future__ import annotations

import pytest
import subprocess
from pathlib import Path

from sonder_runtime.adapters.persistence.durable_continuation import SQLiteDurableContinuationRepository
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.subagents import SubagentBudget, SubagentRequest, SubagentStatus
from sonder_runtime.application.subagents.durable_continuation import DurableContinuationService
from sonder_runtime.adapters.workspace_reality import GitWorkspaceReality

from sonder_runtime.application.execution.resume_reality import (
    ResumeBarrier,
    ResumeMutationBlocked,
    bound_resume_barrier,
    consume_resume_context,
    record_inspection,
    record_replan,
    require_mutation_allowed,
)


def test_unchanged_resume_is_already_mutable_and_delta_is_one_shot():
    barrier = ResumeBarrier({"status": "unchanged", "requires_reinspection": False})
    with bound_resume_barrier(barrier):
        assert consume_resume_context()["status"] == "unchanged"
        assert consume_resume_context() is None
        require_mutation_allowed()


def test_changed_resume_requires_inspection_then_replan():
    barrier = ResumeBarrier({"status": "changed", "requires_reinspection": True, "requires_replan": True})
    with bound_resume_barrier(barrier):
        with pytest.raises(ResumeMutationBlocked):
            require_mutation_allowed()
        record_inspection()
        with pytest.raises(ResumeMutationBlocked):
            require_mutation_allowed()
        record_replan("plan-digest")
        require_mutation_allowed()


def test_unbound_host_acknowledgement_is_refused():
    with pytest.raises(ResumeMutationBlocked):
        record_inspection()
    with pytest.raises(ResumeMutationBlocked):
        record_replan("plan")


def test_request_delta_omits_persistence_snapshot_metadata():
    barrier = ResumeBarrier({"status": "changed", "files": [{"path": "main.py"}],
                             "snapshot": {"dirty": "x" * 20000}, "requires_reinspection": True})
    delta = barrier.consume_context()
    assert "snapshot" not in delta
    assert delta["files"] == [{"path": "main.py"}]


class _Reality:
    def __init__(self, delta=None):
        self.delta = delta
        self.captures = 0

    def capture(self, root, **kwargs):
        self.captures += 1
        return {"version": 1, "root": str(root), "head": "a"}

    def revalidate(self, root, snapshot, owned_paths=(), **kwargs):
        return self.delta


def _request():
    return SubagentRequest(
        "root", "resume", SubagentBudget(max_children=2, max_steps=5, max_wall_seconds=30),
        "child", metadata=(("owned_files", '["main.py"]'),),
    )


def test_real_continuation_persists_snapshot_and_blocks_after_changed_resume(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    reality = _Reality()
    repo = SQLiteDurableContinuationRepository(tmp_path / "child.sqlite")
    service = DurableContinuationService(repo, workspace_reality=reality)
    service.register_root("root", SubagentBudget(max_children=2, max_steps=5, max_wall_seconds=30))

    def first(state, save, control):
        save({"step": 1})
        raise RuntimeError("crash")

    handle = service.spawn(_request(), local_owner_context(correlation_id="first", workspace_roots=(Path(root),)), first)
    assert handle.result(3).status is SubagentStatus.FAILED
    stored = repo.get("child")
    assert stored and stored.checkpoint
    assert "__sonder_host_resume_reality_v1" in stored.checkpoint.state

    reality.delta = {
        "status": "changed", "requires_reinspection": True,
        "requires_replan": True, "files": [{"path": "main.py"}],
    }
    def resumed(state, save, control):
        save({"step": 2})
        require_mutation_allowed()
        return "should be blocked"

    resumed_handle = service.resume(
        "child", local_owner_context(correlation_id="resume", workspace_roots=(Path(root),)),
        resumed, expected_revision=stored.revision,
    )
    assert resumed_handle.result(3).status is SubagentStatus.FAILED
    pending = repo.get("child").checkpoint.state["__sonder_host_resume_reality_v1"]["pending_delta"]
    assert pending["requires_replan"] is True


def _real_git(root):
    root.mkdir()
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    (root / "main.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "commit", "-m", "base"], check=True, capture_output=True)
    return root


@pytest.mark.parametrize("legacy,changed", [(False, False), (False, True), (True, False)])
def test_real_git_durable_resume_retires_evidence_and_gates_mutation(tmp_path, legacy, changed):
    root = _real_git(tmp_path / "repo")
    repo = SQLiteDurableContinuationRepository(tmp_path / "children.db")
    reality = GitWorkspaceReality()
    service = DurableContinuationService(repo, workspace_reality=None if legacy else reality)
    service.register_root("root", SubagentBudget(max_children=2, max_steps=5, max_wall_seconds=30))
    context = local_owner_context(workspace_roots=(root,), correlation_id="real")
    def first(state, save, control):
        save({"step": 1})
        raise RuntimeError("interrupted")
    assert service.spawn(_request(), context, first).result(10).status is SubagentStatus.FAILED
    old = repo.get("child")
    repo.update("child", status=old.status, verification={"verification": ["old"]})
    if changed:
        (root / "main.py").write_text("x = 2\n", encoding="utf-8")
    recovered = DurableContinuationService(repo, workspace_reality=reality)
    seen = []
    def resume(state, save, control):
        delta = consume_resume_context()
        seen.append(delta)
        assert consume_resume_context() is None
        assert repo.get("child").terminal_verification == {}
        if changed or legacy:
            with pytest.raises(ResumeMutationBlocked):
                require_mutation_allowed()
            record_inspection()
            with pytest.raises(ResumeMutationBlocked):
                require_mutation_allowed()
            record_replan("new-plan")
        require_mutation_allowed()
        save({"step": 2})
        return "finished"
    result = recovered.resume("child", context, resume).result(10)
    assert result.status is SubagentStatus.SUCCEEDED, result.error
    assert bool(seen[0]) == (changed or legacy)


def test_actual_conversational_request_consumes_delta_without_prefix_or_history_change(tmp_path):
    from sonder_runtime.adapters.conversational_subagents import conversational_runner_factory
    from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository
    from sonder_runtime.application.session.capture import SessionCaptureService
    from sonder_runtime.application.ports.model_gateway import ModelResponse
    from types import SimpleNamespace
    requests = []
    class Gateway:
        def generate(self, request, context):
            requests.append(request)
            return ModelResponse("done", "test", request.tier, tokens_out=1)
    sessions = SQLiteSessionRepository(tmp_path / "sessions.db")
    runner = conversational_runner_factory(Gateway(), sessions, SessionCaptureService(sessions))(
        _request(), local_owner_context(workspace_roots=(tmp_path,), correlation_id="prompt"),
    )
    saved = []
    barrier = ResumeBarrier({"status": "changed", "files": [{"path": "main.py"}], "requires_reinspection": True})
    with bound_resume_barrier(barrier):
        runner({}, lambda state, cursor: saved.append(state), SimpleNamespace(cancelled=False))
        runner(saved[-1], lambda state, cursor: saved.append(state), SimpleNamespace(cancelled=False))
    assert "HOST RESUME REALITY DELTA" in requests[0].prompt
    assert "HOST RESUME REALITY DELTA" not in requests[1].prompt
    assert requests[0].system == requests[1].system
    assert all("HOST RESUME REALITY DELTA" not in item["content"] for item in saved[-1]["history"])
