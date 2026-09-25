"""Snapshot and drift checks for the six typed build tool descriptors.

One name, one schema, one permission meaning (TOOL-001): the typed build
names never equal a legacy server tool name -- the legacy ``build_run(root,
command)`` keeps its own name and meaning (F2) -- and every schema is closed.
"""
from __future__ import annotations

import pytest

import permission_modes as pm
from sonder_runtime.adapters.build.executor import BUILD_TYPED_TOOLS
from sonder_runtime.bootstrap import native_mcp, typed_tools
from sonder_runtime.bootstrap.native_mcp import native_tool_registry
from sonder_runtime.domain.tools.descriptors import ExecutionClass, ToolEffect

pytestmark = pytest.mark.unit

R, W, X = ToolEffect.READ_FILES, ToolEffect.WRITE_FILES, ToolEffect.EXECUTE

EXPECTED = {
    "build_model": (frozenset({R}), ExecutionClass.PURE, "safe", set()),
    "build_job": (frozenset({R, W, X}), ExecutionClass.HOST, "execution", set()),
    "build_job_result": (frozenset({R}), ExecutionClass.PURE, "safe", {"job_id"}),
    "build_fix": (frozenset({R, W, X}), ExecutionClass.HOST, "execution", {"target"}),
    "build_fix_result": (frozenset({R}), ExecutionClass.PURE, "safe", {"job_id"}),
    "build_fix_restore": (frozenset({R, W}), ExecutionClass.PURE, "mutation", {"job_id"}),
}


def _schema(name):
    return native_tool_registry().require(name).input_schema


def _walk(schema):
    yield schema
    for value in (schema.get("properties") or {}).values():
        yield from _walk(value)
    if isinstance(schema.get("items"), dict):
        yield from _walk(schema["items"])


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_descriptor_effects_classes_grades_and_closed_schemas(name):
    descriptor = native_tool_registry().require(name)
    effects, execution_class, grade, required = EXPECTED[name]
    assert descriptor.effects == effects
    assert descriptor.execution_class is execution_class
    assert pm.risk_of(name) == grade
    schema = descriptor.input_schema
    assert schema["type"] == "object" and schema["additionalProperties"] is False
    assert set(schema.get("required", ())) == required
    for node in _walk(schema):
        if node.get("type") == "string":
            assert "maxLength" in node or "enum" in node or "pattern" in node, (name, node)
        if node.get("type") == "integer":
            assert "minimum" in node and "maximum" in node, (name, node)
        if node.get("type") == "array":
            assert "maxItems" in node, (name, node)


def test_enums_and_bounds_match_the_contract():
    job = _schema("build_job")["properties"]
    assert job["action"]["enum"] == ["configure", "build", "compile_one", "include_trace"]
    assert job["generator"]["enum"] == [
        "Ninja", "Ninja Multi-Config", "Unix Makefiles", "NMake Makefiles",
        "Visual Studio 17 2022", "Visual Studio 16 2019"]
    assert (job["jobs"]["minimum"], job["jobs"]["maximum"]) == (1, 256)
    assert (job["timeout_seconds"]["minimum"], job["timeout_seconds"]["maximum"]) == (30, 7200)
    assert job["wait_seconds"]["maximum"] == 120
    assert job["preset"]["pattern"] == "^[A-Za-z0-9_.-]{1,128}$"
    assert "command" not in job and "argv" not in job and "env" not in job
    model = _schema("build_model")["properties"]
    assert model["detail"]["enum"] == ["summary", "targets", "compile_units", "toolchain", "presets"]
    assert (model["max_items"]["minimum"], model["max_items"]["maximum"]) == (1, 500)
    fix = _schema("build_fix")["properties"]
    assert (fix["attempts"]["minimum"], fix["attempts"]["maximum"]) == (1, 8)
    assert (fix["timeout_seconds"]["minimum"], fix["timeout_seconds"]["maximum"]) == (60, 14400)
    assert fix["editable_globs"]["maxItems"] == 16
    assert fix["editable_globs"]["items"]["maxLength"] == 128
    assert _schema("build_job_result")["properties"]["job_id"]["pattern"] == "^build-job-[0-9a-f]{16,32}$"
    assert _schema("build_fix_result")["properties"]["job_id"]["pattern"] == "^build-fix-[0-9a-f]{16,32}$"
    assert _schema("build_fix_restore")["properties"]["files"]["maxItems"] == 6


def test_the_six_names_are_typed_everywhere_and_executed_by_the_build_executor():
    assert set(BUILD_TYPED_TOOLS) == set(typed_tools.BUILD_TOOLS)
    assert set(typed_tools.BUILD_TOOLS) <= set(native_mcp._TYPED_TOOL_NAMES)
    registry = typed_tools.typed_tool_registry()
    for name in typed_tools.BUILD_TOOLS:
        assert registry.get(name) is not None
        assert typed_tools.GUARD_KNOBS[name] == ()
        assert "bypass" not in registry.get(name).input_schema["properties"]
    rules = {rule.tool for rule in typed_tools.typed_tool_policy().rules}
    assert set(typed_tools.BUILD_TOOLS) <= rules


def test_no_typed_build_name_equals_a_legacy_server_tool_name():
    import server

    legacy = {tool.name for tool in server.mcp._tool_manager.list_tools()}
    assert "build_run" in legacy, "the legacy build_run keeps its own name"
    assert not set(typed_tools.BUILD_TOOLS) & legacy
    assert "build_run" not in native_mcp._TYPED_TOOL_NAMES
    assert not set(typed_tools.BUILD_TOOLS) & set(native_mcp._LEGACY_ALIASES)


def test_execution_sets_are_registered_and_drift_free():
    native = {item.name for item in native_tool_registry().list_all()}
    assert pm.NATIVE_EXECUTION_TOOLS <= native
    assert not pm.NATIVE_EXECUTION_TOOLS & pm.EXECUTION_TOOLS
    for name in pm.NATIVE_EXECUTION_TOOLS:
        assert pm.NATIVE_MCP_WORK[name] == "execution"
    for name in ("build_model", "build_job_result", "build_fix_result", "build_fix_restore"):
        assert name in pm.NATIVE_MCP_WORK and name in native
