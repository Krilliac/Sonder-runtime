"""End-to-end permission and path checks for greenfield build workers.

The model is fake, but dispatch is the production ``server._agent_impl``
loop.  This keeps the tests on the real project scope and tool permission
gates instead of making the fake dispatcher decide whether a write happened.
"""
from __future__ import annotations

import json
import pytest
import permission_modes as pm

from sonder_runtime.adapters import fleet_creations, fleet_workers


def _worker(monkeypatch, project, responses, *, tools=None):
    import server

    queue = iter(responses)
    monkeypatch.setenv("SONDER_SPECULATION", "0")
    monkeypatch.setattr(server, "_serve_target", lambda *a, **k: ("fake-model", False, "", "code"))
    monkeypatch.setattr(server, "_bridge_provider_for_tier", lambda *a, **k: None)
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server.unsafe_lab, "active", lambda: False)
    monkeypatch.setattr(server, "_make_tier_generate", lambda *a, **k: lambda prompt, history=None: next(queue))
    monkeypatch.setattr(
        server, "_make_generate",
        lambda *args, **kwargs: lambda prompt, history=None: next(queue),
    )
    return fleet_workers.repository_worker(
        "code", "", len(responses), build=True, orchestrator=server.master_orchestrator,
        activity=server.activity_tracker, agent_impl=server._agent_impl,
        project_tools=set(server._PROJECT_BOUND_AGENT_TOOLS if tools is None else tools),
        unsafe_active=server.unsafe_lab.active,
    )


def test_real_build_worker_writes_only_inside_issued_worker_root(monkeypatch, tmp_path):
    workspace = fleet_creations.create_workspace("integration-write", 1, state_home=tmp_path)
    project = workspace.workers[0]
    worker = _worker(
        monkeypatch,
        project,
        [
            '{"tool":"directory_tree","args":{"path":"."}}',
            '{"tool":"file_write","args":{"path":"app.py","content":"print(1)\\n"}}',
            '{"tool":"file_read","args":{"path":"app.py"}}',
            '{"final":"created app.py"}',
        ],
    )
    receipt = worker("create an app", str(project))
    assert (project / "app.py").exists(), receipt.output
    assert (project / "app.py").read_text(encoding="utf-8") == "print(1)\n"
    assert receipt.project == str(project.resolve())
    assert "file_write" in receipt.tools


@pytest.mark.parametrize("escape", ["absolute", "sibling"])
def test_real_build_worker_refuses_path_escape(monkeypatch, tmp_path, escape):
    workspace = fleet_creations.create_workspace("integration-escape", 2, state_home=tmp_path)
    project = workspace.workers[0]
    outside = tmp_path / "outside.py" if escape == "absolute" else workspace.workers[1] / "outside.py"
    requested = str(outside) if escape == "absolute" else "../worker-02/outside.py"
    worker = _worker(
        monkeypatch,
        project,
        [
            '{"tool":"directory_tree","args":{"path":"."}}',
            '{"tool":"file_write","args":{"path":%s,"content":"escaped"}}'
            % json.dumps(requested),
            '{"final":"done"}',
        ],
    )
    receipt = worker("create an app", str(project))
    assert not outside.exists()
    assert "outside" in str(receipt.output).lower(), receipt.output


def test_real_build_worker_plan_permission_mode_leaves_no_files(monkeypatch, tmp_path):
    monkeypatch.setattr(pm, "current_mode", lambda: pm.PLAN)
    workspace = fleet_creations.create_workspace("integration-restrict", 1, state_home=tmp_path)
    project = workspace.workers[0]
    worker = _worker(
        monkeypatch,
        project,
        [
            '{"tool":"directory_tree","args":{"path":"."}}',
            '{"tool":"file_write","args":{"path":"blocked.py","content":"bad"}}',
            '{"final":"write refused"}',
        ],
    )
    receipt = worker("create an app", str(project))
    assert list(project.iterdir()) == []
    assert "mode=plan" in receipt.output, receipt.output


def test_build_worker_refuses_unsafe_mode_changed_at_agent_entry(monkeypatch, tmp_path):
    import server
    workspace = fleet_creations.create_workspace("integration-unsafe", 1, state_home=tmp_path)
    project = workspace.workers[0]
    worker = _worker(monkeypatch, project, ['{"final":"must not run"}'])
    # The worker captured the normal-mode check, but the agent observes a new
    # unsafe-mode snapshot. The per-call invariant must still reject it.
    monkeypatch.setattr(server.unsafe_lab, "active", lambda: True)
    with pytest.raises(RuntimeError, match="normal project and permission gates"):
        worker("create an app", str(project))
    assert list(project.iterdir()) == []


@pytest.fixture(autouse=True)
def default_permission_mode(monkeypatch):
    monkeypatch.setattr(pm, "current_mode", lambda: pm.AUTO)
    monkeypatch.setattr(pm, "_rule_lookup", lambda tool: None)
