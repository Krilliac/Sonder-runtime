import json

import server


def test_direct_tool_records_and_returns_json(monkeypatch, tmp_path):
    (tmp_path / "requirements.txt").write_text("requests==2.32.3\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr(server, "_record_direct_tool", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(server, "_file_bypass_allowed", lambda *args: True)

    output = server.dependency_inventory(path=str(tmp_path), extra_roots=str(tmp_path))
    data = json.loads(output)

    assert data["items"][0]["name"] == "requests"
    assert calls[-1][0][0] == "dependency_inventory"
    assert calls[-1][1]["ok"] is True


def test_repository_agent_dispatch_is_project_scoped(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(server, "dependency_inventory", lambda **kwargs: calls.append(kwargs) or "inventory")

    output = server._agent_dispatch(
        "dependency_inventory", {"path": "."}, read_only=True,
        repository_extra_roots=str(tmp_path.resolve()),
    )

    assert output == "inventory"
    assert calls[0]["path"] == str(tmp_path.resolve())
    assert calls[0]["extra_roots"] == str(tmp_path.resolve())
    assert calls[0]["approval"] == server._TRUSTED_REPOSITORY_APPROVAL


def test_dependency_inventory_is_read_only_project_and_autopilot_observe_tool():
    assert "dependency_inventory" in server.REPOSITORY_READ_ONLY_TOOLS
    assert "dependency_inventory" in server._PROJECT_SCOPED_PATH_TOOLS
    assert "dependency_inventory" in server._WORK_INSPECTION_TOOLS
    assert "dependency_inventory" in server._AGENT_DEDUPLICATED_INSPECTION_TOOLS
    assert "dependency_inventory" in server._AUTOPILOT_OBSERVE_TOOLS
    assert "dependency_inventory" in server._WORK_VALIDATION_TOOLS
    assert "dependency_inventory" not in server._WORK_MUTATION_TOOLS
    assert "dependency_inventory" in server.tool_manifest()
    assert "dependency_inventory" in server._agent_tool_help(read_only=True)


def test_read_only_policy_rejects_dependency_inventory_escape(tmp_path):
    output = server._agent_dispatch(
        "dependency_inventory", {"path": "../outside"}, read_only=True,
        repository_extra_roots=str(tmp_path.resolve()),
    )
    assert output.startswith("ERROR: agent project path rejected")
