from __future__ import annotations

import io
import json
import zipfile

import pytest

import sonder_runtime.adapters.filesystem.file_ops as file_ops
from sonder_runtime.adapters.tool_executor import ToolExecutorAdapter
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.tool_executor import ToolCall


@pytest.fixture()
def executor(tmp_path, monkeypatch):
    monkeypatch.setattr(file_ops, "workspace_root", lambda: tmp_path)
    return ToolExecutorAdapter(), tmp_path


def _context():
    return local_owner_context(correlation_id="archive-extract-test")


def _zip(path):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("nested/message.txt", "hello archive")


def test_archive_extract_executor_preserves_transactional_result(executor):
    adapter, workspace = executor
    _zip(workspace / "bundle.zip")

    result = adapter.execute(
        ToolCall("archive_extract", {
            "source": "bundle.zip",
            "destination": "unpacked",
        }),
        _context(),
    )

    assert result.ok is True
    assert json.loads(result.output)["validation_passed"] is True
    assert result.evidence["overwrote"] is False
    assert (workspace / "unpacked" / "nested" / "message.txt").read_text() == "hello archive"


def test_archive_extract_executor_fails_closed_without_replacing_destination(executor):
    adapter, workspace = executor
    _zip(workspace / "bundle.zip")
    destination = workspace / "unpacked"
    destination.mkdir()
    (destination / "keep.txt").write_text("keep")

    result = adapter.execute(
        ToolCall("archive_extract", {
            "source": "bundle.zip",
            "destination": "unpacked",
        }),
        _context(),
    )

    assert result.ok is False
    assert result.error_code == "FileExistsError"
    assert (destination / "keep.txt").read_text() == "keep"
    assert not (destination / "nested" / "message.txt").exists()
