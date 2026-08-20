import json
from pathlib import Path

import sonder_runtime.adapters.filesystem.file_ops as file_ops
import server


def _operations(value=2):
    return [{"op": "test", "path": "/value", "value": 1},
            {"op": "replace", "path": "/value", "value": value}]


def test_mcp_preview_apply_activity_and_error_reporting(monkeypatch, tmp_path):
    monkeypatch.setattr(file_ops, "workspace_root", lambda: tmp_path)
    target = tmp_path / "data.json"
    target.write_text('{"value":1}', encoding="utf-8")
    changes = []
    monkeypatch.setattr(
        server.activity_tracker, "record_file_change",
        lambda action, path, **kwargs: changes.append((action, path, kwargs)),
    )

    preview = json.loads(server.json_patch("data.json", json.dumps(_operations())))
    assert preview["applied"] is False
    assert changes == []
    applied = json.loads(server.json_patch("data.json", json.dumps(_operations()), mode="apply"))
    assert applied["applied"] is True
    assert changes[0][0] == "json_patch"
    assert json.loads(target.read_text(encoding="utf-8")) == {"value": 2}

    error = server.json_patch("data.json", json.dumps([{"op": "test", "path": "/value", "value": 1}]))
    assert error.startswith("ERROR:")


def test_agent_manifest_project_scope_mutation_and_autopilot_registration(tmp_path):
    assert "json_patch" in server.tool_manifest()
    assert "- json_patch:" in server._agent_tool_help()
    assert "json_patch" in server._PROJECT_SCOPED_PATH_TOOLS
    assert "json_patch" in server._PROJECT_BOUND_AGENT_TOOLS
    assert "json_patch" in server._WORK_MUTATION_TOOLS
    assert "json_patch" in server._AUTOPILOT_WORKSPACE_TOOLS
    assert "json_patch" in server._AUTOPILOT_MUTATION_EVIDENCE
    assert "json_patch" not in server._AUTOPILOT_OBSERVE_TOOLS
    assert server._agent_tool_mutates("json_patch", {"mode": "preview"}) is False
    assert server._agent_tool_mutates("json_patch", {"mode": "apply"}) is True
    assert server._agent_dispatch(
        "json_patch", {"path": "x.json", "operations": _operations()},
        read_only=True,
    ).startswith("ERROR:")

    project = tmp_path / "project"
    project.mkdir()
    scoped = server._project_scope_args(
        "json_patch", {"path": "config/data.json", "operations": _operations(), "mode": "apply"},
        str(project),
    )
    assert Path(scoped["path"]) == project / "config" / "data.json"
    assert server._repository_scope_path_error("json_patch", scoped, str(project)) == ""
    scoped["path"] = str(tmp_path / "escape.json")
    assert "outside" in server._repository_scope_path_error("json_patch", scoped, str(project))


def test_project_bound_agent_dispatch_applies_inside_selected_project(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    target = project / "config.json"
    target.write_text('{"value":1}', encoding="utf-8")
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(project))

    output = server._agent_dispatch_observed(
        "json_patch", {"path": "config.json", "operations": _operations(), "mode": "apply"},
        project=str(project),
    )
    assert json.loads(output)["applied"] is True
    assert json.loads(target.read_text(encoding="utf-8")) == {"value": 2}
    records = server._agent_mutation_records(
        "json_patch", {"path": str(target), "mode": "apply"},
    )
    assert records[0]["path"] == server._agent_normalized_path(str(target))
