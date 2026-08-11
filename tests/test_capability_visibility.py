import json

import server


def test_capability_manifest_is_stable_and_informational():
    first = json.loads(server.tool_capability_manifest())
    second = json.loads(server.tool_capability_manifest())
    assert first["sha256"] == second["sha256"]
    assert len(first["sha256"]) == 64
    assert first["tool_count"] >= 180
    assert "informational" in first["authority"]


def test_access_request_preview_never_grants_or_mutates(tmp_path, monkeypatch):
    monkeypatch.setattr(server.file_ops, "resolve_path", lambda path: (_ for _ in ()).throw(
        PermissionError("outside allowed roots")
    ))
    result = json.loads(server.access_request_preview(str(tmp_path), "write"))
    assert result["state"] == "operator_action_required"
    assert result["grant"] is False
    assert "cannot approve" in result["model_authority"]


def test_access_request_preview_rejects_unknown_mode():
    assert server.access_request_preview(".", "execute").startswith("ERROR:")
