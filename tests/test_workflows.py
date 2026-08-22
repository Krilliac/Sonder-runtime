import json
import os
from concurrent.futures import ThreadPoolExecutor

from sonder_runtime.adapters.filesystem import workflow_store
from sonder_runtime.platform import paths as runtime_paths


def test_ensure_workflows_creates_defaults(monkeypatch, tmp_path):
    monkeypatch.setattr(workflow_store, "workspace_root", lambda: str(tmp_path))
    monkeypatch.delenv("SONDER_WORKFLOWS", raising=False)
    monkeypatch.setenv("SONDER_HOME", str(tmp_path / "state"))
    workflows, path = workflow_store.ensure_workflows()
    assert path.endswith("workflows.json")
    assert "status_sweep" in workflows
    assert path == str(tmp_path / "state" / "workflows.json")
    assert not (tmp_path / "workflows.json").exists()


def test_workflow_state_home_uses_canonical_platform_paths(monkeypatch, tmp_path):
    state_home = tmp_path / "canonical-state"
    monkeypatch.setattr(workflow_store, "workspace_root", lambda: str(tmp_path))
    monkeypatch.setattr(runtime_paths, "ensure_home", lambda: state_home)
    monkeypatch.delenv("SONDER_WORKFLOWS", raising=False)

    assert workflow_store.default_path() == str(state_home / "workflows.json")
    assert workflow_store.resolved_path() == str(state_home / "workflows.json")


def test_default_workflows_copy_legacy_install_file_without_rewriting_it(monkeypatch, tmp_path):
    monkeypatch.setattr(workflow_store, "workspace_root", lambda: str(tmp_path))
    monkeypatch.delenv("SONDER_WORKFLOWS", raising=False)
    monkeypatch.setenv("SONDER_HOME", str(tmp_path / "state"))
    legacy = tmp_path / "workflows.json"
    legacy.write_text(json.dumps({
        "legacy_flow": {"description": "keep", "actions": [{"type": "status"}]},
    }), encoding="utf-8")

    workflows, path = workflow_store.ensure_workflows()

    assert workflows["legacy_flow"]["description"] == "keep"
    assert path == str(tmp_path / "state" / "workflows.json")
    assert legacy.read_text(encoding="utf-8").startswith("{")
    workflow_store.save_workflow("new_flow", [{"type": "status"}])
    assert "new_flow" not in legacy.read_text(encoding="utf-8")


def test_explicit_workflow_override_stays_inside_workspace(monkeypatch, tmp_path):
    monkeypatch.setattr(workflow_store, "workspace_root", lambda: str(tmp_path))
    monkeypatch.setenv("SONDER_WORKFLOWS", "operator-workflows.json")
    monkeypatch.setenv("SONDER_HOME", str(tmp_path / "state"))

    workflows, path = workflow_store.ensure_workflows()

    assert "status_sweep" in workflows
    assert path == str(tmp_path / "operator-workflows.json")


def test_save_and_delete_workflow(monkeypatch, tmp_path):
    monkeypatch.setattr(workflow_store, "workspace_root", lambda: str(tmp_path))
    monkeypatch.delenv("SONDER_WORKFLOWS", raising=False)
    monkeypatch.setenv("SONDER_HOME", str(tmp_path / "state"))
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

    monkeypatch.setattr(workflow_store, "workspace_root", lambda: str(tmp_path))
    monkeypatch.delenv("SONDER_WORKFLOWS", raising=False)
    monkeypatch.setenv("SONDER_HOME", str(tmp_path / "state"))
    actions = json.dumps([{"type": "code", "language": "python", "code": "print('wf')"}])
    assert "Saved workflow" in server.workflow_save("demo_flow", actions, "demo")
    out = server.workflow_run("demo_flow")
    assert "workflow: demo_flow" in out
    assert "wf" in out


def test_server_workflow_list(monkeypatch, tmp_path):
    import server

    monkeypatch.setattr(workflow_store, "workspace_root", lambda: str(tmp_path))
    monkeypatch.delenv("SONDER_WORKFLOWS", raising=False)
    monkeypatch.setenv("SONDER_HOME", str(tmp_path / "state"))
    out = server.workflow_list()
    assert "status_sweep" in out


def test_workflow_writes_are_atomic_and_preserve_prior_file(monkeypatch, tmp_path):
    monkeypatch.setattr(workflow_store, "workspace_root", lambda: str(tmp_path))
    path = tmp_path / "workflows.json"
    workflow_store.write_workflows({
        "prior": {"description": "keep", "actions": [{"type": "status"}]},
    }, str(path))
    before = path.read_bytes()

    def fail_replace(_source, _destination):
        raise OSError("replace blocked")

    monkeypatch.setattr(workflow_store.os, "replace", fail_replace)
    try:
        workflow_store.write_workflows({
            "new": {"description": "no", "actions": [{"type": "status"}]},
        }, str(path))
    except OSError as exc:
        assert "replace blocked" in str(exc)
    else:
        raise AssertionError("expected atomic replace failure")
    assert path.read_bytes() == before
    assert list(tmp_path.glob("*.tmp")) == []


def test_workflow_path_rejects_symlink_escape(monkeypatch, tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    link = root / "escape"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        return
    monkeypatch.setattr(workflow_store, "workspace_root", lambda: str(root))
    try:
        workflow_store.write_workflows({}, str(link / "workflows.json"))
    except ValueError as exc:
        assert "inside workspace" in str(exc)
    else:
        raise AssertionError("expected symlink escape rejection")
    assert not (outside / "workflows.json").exists()


def test_concurrent_workflow_saves_do_not_lose_updates(monkeypatch, tmp_path):
    monkeypatch.setattr(workflow_store, "workspace_root", lambda: str(tmp_path))
    monkeypatch.delenv("SONDER_WORKFLOWS", raising=False)
    monkeypatch.setenv("SONDER_HOME", str(tmp_path / "state"))

    def save(index):
        workflow_store.save_workflow(
            "flow_%02d" % index, [{"type": "status"}], "concurrent"
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(save, range(24)))
    workflows = workflow_store.read_workflows()
    assert {"flow_%02d" % index for index in range(24)} <= set(workflows)


def test_workflow_storage_and_action_bounds(monkeypatch, tmp_path):
    monkeypatch.setattr(workflow_store, "workspace_root", lambda: str(tmp_path))
    monkeypatch.setenv("SONDER_HOME", str(tmp_path / "state"))
    too_many = [{"type": "status"}] * (
        workflow_store.MAX_ACTIONS_PER_WORKFLOW + 1
    )
    try:
        workflow_store.save_workflow("bounded", too_many)
    except ValueError as exc:
        assert "action limit" in str(exc)
    else:
        raise AssertionError("expected workflow action-count rejection")

    path = tmp_path / "workflows.json"
    path.write_bytes(b" " * (workflow_store.MAX_WORKFLOW_BYTES + 1))
    try:
        workflow_store.read_workflows(str(path))
    except ValueError as exc:
        assert "byte limit" in str(exc)
    else:
        raise AssertionError("expected oversized workflow-file rejection")


def test_server_reports_corrupt_workflow_storage_as_typed_error(monkeypatch, tmp_path):
    import server

    monkeypatch.setattr(workflow_store, "workspace_root", lambda: str(tmp_path))
    monkeypatch.delenv("SONDER_WORKFLOWS", raising=False)
    monkeypatch.setenv("SONDER_HOME", str(tmp_path / "state"))
    (tmp_path / "workflows.json").write_text("{broken", encoding="utf-8")
    output = server.workflow_list()
    assert output.startswith("ERROR: ")
    assert "Expecting property name" in output


def test_server_failed_workflow_preserves_legacy_wire_text(monkeypatch, tmp_path):
    import server

    monkeypatch.setattr(workflow_store, "workspace_root", lambda: str(tmp_path))
    monkeypatch.delenv("SONDER_WORKFLOWS", raising=False)
    monkeypatch.setenv("SONDER_HOME", str(tmp_path / "state"))
    server.workflow_save("failing_flow", '[{"type":"not_a_real_action"}]')
    output = server.workflow_run("failing_flow")
    assert output.startswith("workflow: failing_flow\n")
    assert "loop status: failed" in output
    assert not output.startswith("ERROR: ")
