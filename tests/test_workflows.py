import json

import workflow_store


def test_ensure_workflows_creates_defaults(monkeypatch, tmp_path):
    monkeypatch.setattr(workflow_store, "workspace_root", lambda: str(tmp_path))
    monkeypatch.delenv("TRILOBITE_WORKFLOWS", raising=False)
    workflows, path = workflow_store.ensure_workflows()
    assert path.endswith("workflows.json")
    assert "status_sweep" in workflows


def test_save_and_delete_workflow(monkeypatch, tmp_path):
    monkeypatch.setattr(workflow_store, "workspace_root", lambda: str(tmp_path))
    monkeypatch.delenv("TRILOBITE_WORKFLOWS", raising=False)
    wf, _ = workflow_store.save_workflow(
        "my_flow",
        [{"type": "code", "code": "print(1)"}],
        "demo",
    )
    assert wf["description"] == "demo"
    assert workflow_store.get_workflow("my_flow")["actions"][0]["type"] == "code"
    existed, _ = workflow_store.delete_workflow("my_flow")
    assert existed is True
    assert workflow_store.get_workflow("my_flow") is None


def test_invalid_workflow_name_rejected():
    try:
        workflow_store.normalize_name("1 nope")
    except ValueError as e:
        assert "invalid workflow name" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_server_workflow_save_and_run(monkeypatch, tmp_path):
    import server

    monkeypatch.setattr(server.workflow_store, "workspace_root", lambda: str(tmp_path))
    monkeypatch.delenv("TRILOBITE_WORKFLOWS", raising=False)
    actions = json.dumps([{"type": "code", "language": "python", "code": "print('wf')"}])
    assert "Saved workflow" in server.workflow_save("demo_flow", actions, "demo")
    out = server.workflow_run("demo_flow")
    assert "workflow: demo_flow" in out
    assert "wf" in out


def test_server_workflow_list(monkeypatch, tmp_path):
    import server

    monkeypatch.setattr(server.workflow_store, "workspace_root", lambda: str(tmp_path))
    monkeypatch.delenv("TRILOBITE_WORKFLOWS", raising=False)
    out = server.workflow_list()
    assert "status_sweep" in out
