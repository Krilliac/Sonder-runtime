"""TOOL-006: the five debug tools are named identically on every catalog."""
from __future__ import annotations

import pytest

import permission_modes as pm
from sonder_runtime.adapters.debug_tools_executor import DEBUG_TYPED_TOOLS, DebugToolExecutor
from sonder_runtime.bootstrap import debug_tools, native_mcp, typed_tools
from sonder_runtime.domain.tools.descriptors import ExecutionClass, ToolEffect

pytestmark = pytest.mark.unit

FIVE = {"crash_triage", "crash_digest", "profile_digest", "profile_capture_digest", "debug_run_result"}


def test_every_catalog_lists_the_same_five_names():
    assert set(DEBUG_TYPED_TOOLS) == FIVE
    assert set(debug_tools.DEBUG_TYPED_TOOLS) == FIVE
    assert set(typed_tools.DEBUG_TOOLS) == FIVE
    assert {descriptor.name for descriptor in native_mcp._DEBUG_TOOLS} == FIVE
    assert FIVE <= native_mcp._TYPED_TOOL_NAMES
    assert FIVE <= set(typed_tools.TYPED_TOOLS)
    assert FIVE <= set(typed_tools.GUARD_KNOBS)
    assert DebugToolExecutor.NAMES == FIVE
    graded = {name for name in FIVE if name in pm.NATIVE_MCP_WORK} | (FIVE & pm.EXECUTION_TOOLS)
    assert graded == FIVE


def test_grades_split_pure_from_host_tools():
    assert {name for name in FIVE if pm.NATIVE_MCP_WORK.get(name) == "safe"} == {
        "crash_triage", "profile_digest", "debug_run_result"}
    assert FIVE & pm.EXECUTION_TOOLS == {"crash_digest", "profile_capture_digest"}


def test_the_typed_registry_and_policy_admit_them():
    registry = typed_tools.typed_tool_registry()
    policy = typed_tools.typed_tool_policy()
    for name in FIVE:
        descriptor = registry.get(name)
        assert descriptor is not None, name
        assert descriptor.input_schema["additionalProperties"] is False
        assert any(rule.tool == name for rule in policy.rules), name


def test_descriptors_match_the_spec_table():
    by_name = {descriptor.name: descriptor for descriptor in native_mcp._DEBUG_TOOLS}
    assert by_name["crash_triage"].effects == frozenset({ToolEffect.READ_FILES})
    assert by_name["crash_triage"].execution_class is ExecutionClass.PURE
    assert by_name["profile_digest"].execution_class is ExecutionClass.PURE
    assert by_name["crash_digest"].effects == frozenset({
        ToolEffect.READ_FILES, ToolEffect.WRITE_FILES, ToolEffect.EXECUTE, ToolEffect.NETWORK})
    assert by_name["profile_capture_digest"].effects == frozenset({
        ToolEffect.READ_FILES, ToolEffect.WRITE_FILES, ToolEffect.EXECUTE})
    assert by_name["debug_run_result"].effects == frozenset()
    crash = by_name["crash_digest"].input_schema["properties"]
    assert crash["engine"]["enum"] == ["auto", "cdb", "gdb", "lldb", "eu_stack",
                                       "minidump_stackwalk", "llvm_symbolizer", "pure"]
    assert crash["symbol_dirs"]["maxItems"] == 8
    assert (crash["timeout_seconds"]["minimum"], crash["timeout_seconds"]["maximum"]) == (10, 900)
    assert crash["wait_seconds"]["maximum"] == 120
    assert "--symbols-online" in by_name["crash_digest"].description
    assert "directory" in by_name["crash_triage"].description
    assert "CAPTURE_NEEDS_HOST_TOOL" in by_name["profile_digest"].description
    result = by_name["debug_run_result"].input_schema
    assert result["required"] == ["run_id"] and "cancel" not in result["properties"]
    assert result["properties"]["wait_seconds"]["maximum"] == 60
    for descriptor in native_mcp._DEBUG_TOOLS:
        schema = descriptor.input_schema
        assert "argv" not in schema["properties"] and "store" not in str(schema)
