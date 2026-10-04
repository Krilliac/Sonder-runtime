from __future__ import annotations

import pytest
from pathlib import Path
from dataclasses import dataclass

from sonder_runtime.adapters import fleet_creations
from sonder_runtime.adapters import fleet_workers


def test_create_workspace_allocates_private_worker_roots(tmp_path):
    workspace = fleet_creations.create_workspace("master-123", 2, state_home=tmp_path)
    assert workspace.root == tmp_path / "creations" / "master-123"
    assert [path.name for path in workspace.workers] == ["worker-01", "worker-02"]


def test_attach_receipt_reports_only_files_inside_worker_root(tmp_path):
    workspace = fleet_creations.create_workspace("master-123", 1, state_home=tmp_path)
    worker = workspace.workers[0]
    (worker / "app.py").write_text("print('ok')", encoding="utf-8")
    @dataclass
    class Result:
        produced_files: tuple = ()
        checks_run: bool = False
        checks_passed: bool | None = None
    result = Result()
    attached = fleet_creations.attach_receipt(result, worker)
    assert attached.produced_files == ("app.py",)
    assert str(tmp_path / "outside.txt") not in attached.produced_files


def test_symlink_output_is_rejected(tmp_path):
    workspace = fleet_creations.create_workspace("master-123", 1, state_home=tmp_path)
    worker = workspace.workers[0]
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = worker / "escape.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this Windows account")
    with pytest.raises(PermissionError):
        fleet_creations.attach_receipt(
            type("Result", (), {"produced_files": ()})(), worker,
        )


def test_workspace_rejects_master_id_path_escape(tmp_path):
    with pytest.raises(ValueError):
        fleet_creations.create_workspace("..", 1, state_home=tmp_path)


def test_attach_receipt_rejects_unprovisioned_path_even_when_it_exists(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    result = type("Result", (), {"produced_files": ()})()
    with pytest.raises(PermissionError, match="not provisioned"):
        fleet_creations.attach_receipt(result, outside)


def test_build_worker_uses_only_project_bound_tools_and_scope(tmp_path):
    workspace = fleet_creations.create_workspace("master-tools", 1, state_home=tmp_path)
    project = workspace.workers[0]
    calls = []

    class Activity:
        def current_response_id(self):
            return "response-1"

        class _Binding:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        def bind_response(self, _response_id):
            return self._Binding()

    class Orchestrator:
        def resolve_repository_project_root(self, _prompt, assigned):
            return str(Path(assigned).resolve())

        def same_project_root(self, left, right):
            return str(Path(left).resolve()) == str(Path(right).resolve())

        def current_worker_cancel_requested(self, _agent_id):
            return False

        def repository_worker_result(self, receipt, expected):
            assert receipt.project_scope == expected
            return receipt

    class Receipt:
        project_scope = str(project.resolve())
        tools = ("file_write", "workspace_run")
        output = "=== TOOL EVIDENCE ==="

    def agent_impl(prompt, **kwargs):
        calls.append((prompt, kwargs))
        (project / "created.txt").write_text("ok", encoding="utf-8")
        return Receipt()

    worker = fleet_workers.repository_worker(
        "code", "", 8, build=True, orchestrator=Orchestrator(),
        activity=Activity(), agent_impl=agent_impl,
        project_tools={"file_write", "workspace_run", "web_fetch"},
        unsafe_active=lambda: False,
    )
    result = worker("create an app", str(project))
    assert result.project_scope == str(project.resolve())
    assert calls[0][1]["allow_web"] is False
    assert calls[0][1]["read_only"] is False
    assert calls[0][1]["tool_allowlist"] == {"file_write", "workspace_run"}
    assert (project / "created.txt").read_text(encoding="utf-8") == "ok"


def test_build_worker_refuses_scope_escape_before_model_call(tmp_path):
    workspace = fleet_creations.create_workspace("master-scope", 1, state_home=tmp_path)
    project = workspace.workers[0]
    outside = tmp_path / "outside"
    outside.mkdir()
    calls = []

    class Activity:
        def current_response_id(self):
            return "response-1"

        class _Binding:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        def bind_response(self, _response_id):
            return self._Binding()

    class Orchestrator:
        def resolve_repository_project_root(self, prompt, assigned):
            return str(outside if "escape" in prompt else project)

        def same_project_root(self, left, right):
            return str(left) == str(right)

    def agent_impl(*args, **kwargs):
        calls.append(True)

    worker = fleet_workers.repository_worker(
        "code", "", 8, build=True, orchestrator=Orchestrator(),
        activity=Activity(), agent_impl=agent_impl,
        project_tools={"file_write"}, unsafe_active=lambda: False,
    )
    with pytest.raises(RuntimeError, match="assignment changed"):
        worker("escape", str(project))
    assert calls == []


def test_greenfield_build_rejects_unprovisioned_assignment_before_model_call(tmp_path):
    fleet_creations.create_workspace("master-host-owned", 1, state_home=tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    calls = []

    class Activity:
        def current_response_id(self):
            return "response-1"

        class _Binding:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        def bind_response(self, _response_id):
            return self._Binding()

    class Orchestrator:
        def resolve_repository_project_root(self, _prompt, assigned):
            return str(Path(assigned).resolve())

        def same_project_root(self, left, right):
            return str(Path(left).resolve()) == str(Path(right).resolve())

    def agent_impl(*args, **kwargs):
        calls.append(True)

    worker = fleet_workers.repository_worker(
        "code", "", 8, build=True, orchestrator=Orchestrator(),
        activity=Activity(), agent_impl=agent_impl,
        project_tools={"file_write"}, unsafe_active=lambda: False,
    )
    with pytest.raises(RuntimeError, match="not host-provisioned"):
        worker("create", str(outside))
    assert calls == []


def test_restrictive_build_tool_mode_does_not_create_files(tmp_path):
    workspace = fleet_creations.create_workspace("master-restricted", 1, state_home=tmp_path)
    project = workspace.workers[0]
    calls = []

    class Activity:
        def current_response_id(self):
            return "response-1"

        class _Binding:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        def bind_response(self, _response_id):
            return self._Binding()

    class Orchestrator:
        def resolve_repository_project_root(self, _prompt, assigned):
            return str(Path(assigned).resolve())

        def same_project_root(self, left, right):
            return str(Path(left).resolve()) == str(Path(right).resolve())

        def current_worker_cancel_requested(self, _agent_id):
            return False

        def repository_worker_result(self, receipt, expected):
            return receipt

    class Receipt:
        project_scope = str(project.resolve())
        tools = ()
        output = "=== TOOL EVIDENCE ==="

    def agent_impl(prompt, **kwargs):
        calls.append(kwargs)
        # A correctly gated fake model must observe that mutation is absent.
        if "file_write" in kwargs["tool_allowlist"]:
            (project / "must-not-exist.txt").write_text("bad", encoding="utf-8")
        return Receipt()

    worker = fleet_workers.repository_worker(
        "code", "", 8, build=True, orchestrator=Orchestrator(),
        activity=Activity(), agent_impl=agent_impl,
        project_tools={"file_read"}, unsafe_active=lambda: False,
    )
    worker("create", str(project))
    assert calls[0]["tool_allowlist"] == {"file_read"}
    assert not (project / "must-not-exist.txt").exists()
