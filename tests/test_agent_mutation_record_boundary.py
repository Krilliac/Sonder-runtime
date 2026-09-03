"""Boundary tests for mutation_record in sonder_runtime.adapters.agent_work_coverage."""

import server
from sonder_runtime.adapters.agent_work_coverage import mutation_record


def test_root_helper_is_identity_preserving_alias():
    assert server._agent_mutation_record is mutation_record


def test_returns_first_record():
    result = mutation_record("file_write", {"path": "/tmp/test.txt"})
    assert isinstance(result, dict)
    assert result["tool"] == "file_write"
    assert result["path"]


def test_default_for_unknown_tool():
    result = mutation_record("memory_search", {})
    assert result == {"tool": "memory_search", "path": ""}


def test_default_for_empty_args():
    result = mutation_record("file_write", {})
    assert isinstance(result, dict)
    assert result["tool"] == "file_write"
