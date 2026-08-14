import json
import hashlib

import server


def test_capability_manifest_is_stable_and_informational():
    first = json.loads(server.tool_capability_manifest())
    second = json.loads(server.tool_capability_manifest())
    assert first["sha256"] == second["sha256"]
    assert len(first["sha256"]) == 64
    assert first["tool_count"] >= 180
    assert len(first["tools"]) == first["tool_count"]
    assert first["tools"] == sorted(first["tools"], key=lambda tool: tool["name"])
    assert all(
        isinstance(tool["name"], str)
        and isinstance(tool["description"], str)
        and isinstance(tool["parameters"], dict)
        for tool in first["tools"]
    )
    canonical = json.dumps(
        first["tools"], ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    )
    assert first["sha256"] == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert "informational" in first["authority"]


def test_capability_manifest_exposes_required_arguments_for_a_live_tool():
    payload = json.loads(server.tool_capability_manifest())
    tools = {tool["name"]: tool for tool in payload["tools"]}

    preview = tools["access_request_preview"]
    assert preview["parameters"]["required"] == ["path"]
    assert preview["parameters"]["properties"]["path"]["type"] == "string"


def test_access_request_preview_never_grants_or_mutates(tmp_path, monkeypatch):
    monkeypatch.setattr(server.file_ops, "resolve_path", lambda path: (_ for _ in ()).throw(
        PermissionError("outside allowed roots")
    ))
    result = json.loads(server.access_request_preview(str(tmp_path), "write"))
    assert result["state"] == "operator_action_required"
    assert result["grant"] is False
    assert "cannot approve" in result["model_authority"]


def test_access_request_preview_serializes_already_authorized_path(tmp_path, monkeypatch):
    monkeypatch.setattr(server.file_ops, "resolve_path", lambda path: tmp_path)
    result = json.loads(server.access_request_preview(str(tmp_path), "read"))
    assert result["state"] == "already_authorized"
    assert result["path"] == str(tmp_path)


def test_access_request_preview_rejects_unknown_mode():
    assert server.access_request_preview(".", "execute").startswith("ERROR:")
