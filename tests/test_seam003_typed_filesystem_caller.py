"""SEAM-003 bounded caller migration proof for ``read_file``."""
import os
from pathlib import Path

import pytest

from sonder_runtime.adapters import tool_executor
from sonder_runtime.adapters.filesystem import file_ops
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.tool_executor import ToolCall


@pytest.fixture()
def executor(tmp_path, monkeypatch):
    monkeypatch.setattr(file_ops, "workspace_root", lambda: tmp_path)
    monkeypatch.setattr(file_ops.runtime_paths, "default_home", lambda: tmp_path / "home")
    return tool_executor.ToolExecutorAdapter()


def test_read_file_routes_through_typed_filesystem_provider(executor, tmp_path, monkeypatch):
    target = tmp_path / "note.txt"
    target.write_text("typed filesystem\n", encoding="utf-8")
    calls = []

    class Spy:
        def read(self, request, context):
            calls.append((request, context))
            from sonder_runtime.adapters.filesystem.typed import GuardedFileSystemAdapter

            return GuardedFileSystemAdapter().read(request, context)

    monkeypatch.setattr(
        tool_executor,
        "GuardedFileSystemAdapter",
        Spy,
        raising=False,
    )
    result = executor.execute(
        ToolCall("read_file", {"path": "note.txt", "max_bytes": 64}),
        local_owner_context(correlation_id="seam003-test"),
    )

    assert result.ok
    assert result.output == "typed filesystem" + os.linesep
    assert len(calls) == 1
    request, context = calls[0]
    assert request.resource.path == Path("note.txt")
    assert request.max_bytes == 64
    assert request.operation.value == "read"
    assert context.correlation_id == "seam003-test"


def test_typed_read_preserves_guarded_sensitive_path_rejection(executor):
    result = executor.execute(
        ToolCall("read_file", {"path": "server.py"}),
        local_owner_context(correlation_id="seam003-sensitive"),
    )

    assert result.ok is False
    assert result.error_code == "PermissionError"
