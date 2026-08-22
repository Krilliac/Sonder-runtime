from __future__ import annotations

import json

import sonder_runtime.adapters.filesystem.file_ops as file_ops
from sonder_runtime.adapters.tool_executor import ToolExecutorAdapter
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.tool_executor import ToolCall


def test_archive_create_routes_through_typed_adapter(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    (root / "payload.txt").write_text("payload", encoding="utf-8")
    monkeypatch.setattr(file_ops, "workspace_root", lambda: root)

    result = ToolExecutorAdapter().execute(
        ToolCall("archive_create", {
            "root": ".",
            "inputs_json": ["payload.txt"],
            "destination": "bundle.zip",
        }),
        local_owner_context(correlation_id="archive-create-executor"),
    )

    assert result.ok is True
    assert json.loads(result.output)["ok"] is True
    assert result.evidence["overwrote"] is False
    assert (root / "bundle.zip").is_file()


def test_archive_create_does_not_replace_existing_destination(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    (root / "payload.txt").write_text("payload", encoding="utf-8")
    (root / "bundle.zip").write_bytes(b"existing")
    monkeypatch.setattr(file_ops, "workspace_root", lambda: root)

    result = ToolExecutorAdapter().execute(
        ToolCall("archive_create", {
            "root": ".",
            "inputs_json": ["payload.txt"],
            "destination": "bundle.zip",
        }),
        local_owner_context(correlation_id="archive-create-existing"),
    )

    assert result.ok is False
    assert result.error_code == "FileExistsError"
    assert (root / "bundle.zip").read_bytes() == b"existing"
