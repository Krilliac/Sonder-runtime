"""The developer tools on the native MCP surface."""
from __future__ import annotations

import json

import pytest

import permission_modes as pm
from sonder_runtime.application.tools.discovery import ToolDiscovery
from sonder_runtime.bootstrap.native_mcp import native_tool_registry
from tests.test_tools_test_runs_fakes import native

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_operator_rules(monkeypatch):
    monkeypatch.setattr(pm, "_rule_lookup", lambda _tool: None)


def test_the_native_catalog_lists_the_developer_tools(tmp_path):
    names = {item.name for item in native_tool_registry().list_all()}
    assert {"tool_inventory", "test_run", "test_run_result", "output_digest"} <= names
    replies, _, _ = native(tmp_path, [("tools/list", {})])
    listed = {item["name"] for item in replies[0]["result"]["tools"]}
    assert {"tool_inventory", "test_run", "test_run_result", "output_digest"} <= listed


def test_progressive_discovery_finds_and_loads_test_run(tmp_path, monkeypatch):
    monkeypatch.setitem(pm._STATE, "mode", pm.AUTO)
    digest = ToolDiscovery(native_tool_registry()).digest
    replies, audit, developer = native(tmp_path, [
        ("tools/call", {"name": "tool_search", "arguments": {"query": "run tests"}}),
        ("tools/call", {"name": "test_run", "arguments": {}}),
        ("tools/call", {"name": "tool_schema", "arguments": {"names": ["test_run"],
                                                             "inventory_digest": digest}}),
        ("tools/call", {"name": "test_run", "arguments": {"runner": "pytest", "wait_seconds": 0}}),
    ], progressive=True)
    found = [item["name"] for item in json.loads(replies[0]["result"]["output"])["matches"]]
    assert "test_run" in found
    assert replies[1]["result"]["error"] == "tool_not_visible"
    assert replies[3]["result"]["isError"] is False, replies[3]
    assert json.loads(replies[3]["result"]["output"])["status"] == "running"
    records = audit.read()
    typed = [row for row in records if row["tool_name"] == "test_run" and row["success"]]
    assert typed and "developer:test_run" in typed[-1]["policy_match"]


def test_a_schema_violation_is_refused_before_any_planning(tmp_path, monkeypatch):
    monkeypatch.setitem(pm._STATE, "mode", pm.AUTO)
    replies, _, developer = native(tmp_path, [
        ("tools/call", {"name": "test_run", "arguments": {"argv": ["rm", "-rf", "/"]}}),
        ("tools/call", {"name": "test_run", "arguments": {"runner": "tox"}}),
        ("tools/call", {"name": "tool_inventory", "arguments": {"category": "weapons"}}),
    ])
    for reply in replies:
        assert "error" in reply or reply["result"]["isError"], reply
    assert developer.test_runs.planned == []


def test_calls_route_through_the_typed_gateway_with_a_receipt(tmp_path):
    replies, audit, developer = native(tmp_path, [
        ("tools/call", {"name": "tool_inventory", "arguments": {"category": "test_runner"}}),
        ("tools/call", {"name": "output_digest", "arguments": {"job_id": "test-run-" + "b" * 32}}),
    ])
    assert replies[0]["result"]["isError"] is False
    assert json.loads(replies[0]["result"]["output"])["tools"][0]["name"] == "pytest"
    assert replies[1]["result"]["isError"] is False
    assert developer.inventory.calls == [("test_runner", None, False, True)]
    receipts = {row["tool_name"]: row for row in audit.read()}
    assert receipts["tool_inventory"]["success"] is True
    assert receipts["tool_inventory"]["source"] == "mcp"
    assert "developer:tool_inventory" in receipts["tool_inventory"]["policy_match"]
    assert receipts["output_digest"]["success"] is True
