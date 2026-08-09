from dataclasses import replace
import inspect

import pytest

import server
import tool_capabilities as capabilities
from scripts import package_local_system as package


def test_initial_shadow_registry_is_immutable_and_has_no_drift():
    assert set(capabilities.CAPABILITIES) == {
        "environment_status", "hardware_profile", "file_policy",
        "workspace_inventory", "directory_tree", "file_find", "file_read",
        "file_read_range", "file_digest", "text_search", "repo_status",
        "repo_diff",
    }
    with pytest.raises(TypeError):
        capabilities.CAPABILITIES["new"] = capabilities.CAPABILITIES["file_read"]
    assert "tool_capabilities.py" in package.REQUIRED_FILES
    issues = capabilities.validate_shadow(server._tool_capability_shadow_surfaces())
    assert issues == ()


def test_local_only_tools_are_absent_from_hosted_help_and_policy_surface():
    surface = server._tool_capability_shadow_surfaces()
    local_only = {
        name for name, descriptor in capabilities.CAPABILITIES.items()
        if descriptor.cloud is capabilities.CloudRequirement.LOCAL_ONLY
    }
    assert local_only == server._CLOUD_AGENT_LOCAL_ONLY_TOOLS
    assert local_only.isdisjoint(surface.hosted_agent_tools)
    for name in local_only:
        assert "- %s:" % name in surface.full_agent_help
        assert "- %s:" % name not in surface.hosted_agent_help
        error = server._cloud_agent_tool_policy_error(name)
        assert "local-only tool" in error
        assert "private workspace or machine data" in error


def test_shadow_validator_reports_allow_help_dispatch_and_metadata_drift():
    surface = server._tool_capability_shadow_surfaces()
    changed = replace(
        surface,
        tool_manifest="",
        repository_read_only_tools=surface.repository_read_only_tools - {"file_read"},
        dispatch_tools=surface.dispatch_tools - {"file_read"},
        deduplicated_inspection_tools=surface.deduplicated_inspection_tools - {"file_read"},
        hosted_agent_help=surface.hosted_agent_help + "\n- file_read: unsafe drift",
    )
    issues = capabilities.validate_shadow(changed)
    assert any("file_read: tool manifest is absent" in issue for issue in issues)
    assert any("file_read: repository read-only allow-list is absent" in issue for issue in issues)
    assert any("file_read: agent dispatcher is absent" in issue for issue in issues)
    assert any("file_read: deduplicated inspection set is absent" in issue for issue in issues)
    assert any("file_read: hosted-agent help is present" in issue for issue in issues)


def test_descriptor_policy_invariants_are_checked():
    unsafe = replace(
        capabilities.CAPABILITIES["file_read"],
        effect=capabilities.Effect.MUTATION,
        permission=capabilities.Permission.NONE,
    )
    issues = capabilities.validate_shadow(
        server._tool_capability_shadow_surfaces(), descriptors=[unsafe],
    )
    assert any("mutation cannot have permission=none" in issue for issue in issues)


def test_diagnostics_exposes_shadow_result_without_startup_enforcement():
    report = server.tool_capability_shadow_report()
    assert report == "ok (12 descriptors; shadow-only)"
    # Prove diagnostics consumes the shadow report without running its unrelated
    # model, database, NPU, and filesystem checks in this focused unit test.
    source = inspect.getsource(server.diagnostics)
    assert "tool_capability_shadow_report()" in source
    assert "tool capability shadow" in source
