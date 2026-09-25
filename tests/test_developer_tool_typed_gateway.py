"""The developer tools in the typed tool registry, policy and executor."""
from __future__ import annotations

import json
import uuid

import pytest

import permission_modes as pm
from sonder_runtime.adapters.developer_tools_executor import DEVELOPER_TYPED_TOOLS, DeveloperToolExecutor
from sonder_runtime.application.ports.tool_registry import ToolCall
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.tools.gateway_contract import (
    ToolGatewayRequest,
    ToolPermission,
    ToolScope,
)
from sonder_runtime.bootstrap import developer_tools, native_mcp, typed_tools
from sonder_runtime.domain.tools.descriptors import ExecutionClass
from tests.test_tools_test_runs_fakes import FakeDigest, facade, services

pytestmark = pytest.mark.unit


def test_the_registry_carries_the_four_tools_with_the_native_schemas():
    registry = typed_tools.typed_tool_registry()
    native = native_mcp.native_tool_registry()
    for name in DEVELOPER_TYPED_TOOLS:
        typed, declared = registry.get(name), native.get(name)
        assert typed is not None and declared is not None, name
        assert typed.input_schema == declared.input_schema
        assert typed.effects == declared.effects
        assert typed.execution_class == declared.execution_class
    assert set(typed_tools.DEVELOPER_TOOLS) == set(DEVELOPER_TYPED_TOOLS)
    assert set(developer_tools.DEVELOPER_TYPED_TOOLS) == set(DEVELOPER_TYPED_TOOLS)
    assert set(typed_tools.DEVELOPER_TOOLS) <= native_mcp._TYPED_TOOL_NAMES
    assert native.get("test_run").execution_class is ExecutionClass.HOST
    assert native.get("test_run").input_schema["additionalProperties"] is False


def test_the_policy_admits_them():
    policy = typed_tools.typed_tool_policy()
    names = {rule.tool for rule in policy.rules if rule.rule_id.startswith("developer:")}
    assert all(rule.decision.name == "ALLOW" for rule in policy.rules if rule.tool in names)
    assert names == set(DEVELOPER_TYPED_TOOLS)


def _request(tool, arguments, source="repl"):
    descriptor = typed_tools.typed_tool_registry().get(tool)
    effects = frozenset(effect.name.lower() for effect in descriptor.effects)
    return ToolGatewayRequest(
        request_id=uuid.uuid4().hex, tool_name=tool, arguments=arguments,
        scope=ToolScope("local-owner", (), effects, source=source),
        permission=ToolPermission(effects), execution_world="local",
    )


def test_the_executor_routes_and_the_fallback_still_serves_read_file(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "_rule_lookup", lambda _tool: None)
    tools, audit, developer = facade(tmp_path)
    receipt = tools.execute(_request("tool_inventory", {"category": "compiler"}))
    assert receipt.success, receipt.error
    body = json.loads(receipt.output)
    assert [tool["name"] for tool in body["tools"]] == ["gcc"] and body["ok"] is True
    assert developer.inventory.calls[-1] == ("compiler", None, False, True)
    bad = tools.execute(_request("tool_inventory", {"category": "compiler"} | {"name": "x" * 64}))
    assert bad.success
    receipt = tools.execute(_request("output_digest", {"path": "logs/build.log", "tail_lines": 3}))
    assert receipt.success and json.loads(receipt.output)["tail"] == ["line"] * 3
    refused = tools.execute(_request("output_digest", {"path": "a/.env"}))
    assert not refused.success and refused.error_code == "DIGEST_SOURCE_REJECTED"
    both = tools.execute(_request("output_digest", {"path": "x", "job_id": "test-run-1"}))
    assert not both.success and both.error_code == "DIGEST_SOURCE_REJECTED"
    missing = tools.execute(_request("test_run_result", {"job_id": "test-run-" + "0" * 32}))
    assert not missing.success and missing.error_code == "JOB_NOT_FOUND"
    # the fallback executor is untouched
    root = tmp_path / "ws"
    root.mkdir()
    (root / "notes.txt").write_text("hello\n")
    from sonder_runtime.adapters.filesystem import file_ops

    monkeypatch.setattr(file_ops, "workspace_root", lambda: root)
    read = tools.execute(_request("read_file", {"path": "notes.txt"}))
    assert read.success and "hello" in read.output
    assert all(record["tool_name"] for record in audit.read())


def test_an_uncomposed_runtime_reports_the_tools_unavailable():
    executor = DeveloperToolExecutor(None, fallback=None)
    descriptor = typed_tools.typed_tool_registry().get("tool_inventory")
    result = executor.execute(descriptor, ToolCall("tool_inventory", {}),
                              local_owner_context(correlation_id="c"), ExecutionClass.HOST)
    assert not result.success and result.error_code == "DEVELOPER_TOOLS_UNAVAILABLE"
    assert json.loads(result.output) == {"ok": False, "error_code": "DEVELOPER_TOOLS_UNAVAILABLE",
                                         "message": "developer tools are not composed in this runtime"}
    assert "ERROR:" not in result.output


def test_executor_payloads_stay_under_48000_bytes():
    class HugeDigest(FakeDigest):
        def digest_file(self, path, context, **kwargs):
            from tests.test_tools_test_runs_fakes import Digest

            return Digest({"final_line": "x", "tail": ["y" * 300] * 400,
                           "failure_lines": ["z" * 300] * 400})

    developer = services()
    developer = type(developer)(developer.inventory, developer.test_runs, HugeDigest())
    executor = DeveloperToolExecutor(developer, fallback=None)
    descriptor = typed_tools.typed_tool_registry().get("output_digest")
    result = executor.execute(descriptor, ToolCall("output_digest", {"path": "big.log"}),
                              local_owner_context(correlation_id="c"), ExecutionClass.PURE)
    assert result.success
    assert len(result.output.encode("utf-8")) <= 48_000
    assert json.loads(result.output)["truncated"] is True


def test_the_test_run_arguments_are_the_only_inputs(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "_rule_lookup", lambda _tool: None)
    monkeypatch.setitem(pm._STATE, "mode", pm.AUTO)
    tools, _, developer = facade(tmp_path)
    receipt = tools.execute(_request("test_run", {"runner": "pytest", "selector": "k:fast",
                                                  "wait_seconds": 500, "timeout_seconds": 5}))
    assert receipt.success, receipt.error
    request, wait, _ = developer.test_runs.runs[-1]
    assert wait == 120  # clamped to the maximum wait
    assert request.timeout_seconds == 10 and request.selector == "k:fast"
    assert json.loads(receipt.output)["status"] == "running"
