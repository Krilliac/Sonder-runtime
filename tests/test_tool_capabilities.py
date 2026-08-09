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


def _hosted_generate(responses, prompts):
    def generate(prompt, history=None):
        prompts.append(prompt)
        generate.last_usage = {"tokens_out": 4}
        generate.last_response_meta = {"done_reason": "stop"}
        return responses.pop(0)

    generate.last_usage = {}
    generate.last_response_meta = {}
    generate.num_predict_override = None
    return generate


@pytest.mark.parametrize("tool", sorted(capabilities.CAPABILITIES))
def test_hosted_agent_loop_denies_every_local_only_tool(monkeypatch, tool):
    responses = [
        server.json.dumps({"tool": tool, "args": {}}),
        '{"final":"blocked"}',
    ]
    prompts = []
    dispatches = []
    systems = []
    generate = _hosted_generate(responses, prompts)

    monkeypatch.setattr(
        server, "_serve_target",
        lambda *args, **kwargs: (
            "glm-5.2:cloud", True, False, "cloud-general"
        ),
    )
    monkeypatch.setattr(
        server, "_make_generate",
        lambda model, system, *args, **kwargs: systems.append(system) or generate,
    )
    monkeypatch.setattr(
        server.environment_probe, "agent_brief",
        lambda: (_ for _ in ()).throw(
            AssertionError("host inventory reached hosted system prompt")
        ),
    )
    monkeypatch.setattr(
        server, "_agent_dispatch_observed",
        lambda *args, **kwargs: dispatches.append((args, kwargs)) or "private",
    )

    output = server._agent_impl(
        "exercise the available hosted tools",
        tier="cloud-general",
        max_steps=2,
        include_evidence=True,
    )

    assert not dispatches
    assert "local-only tool '%s'" % tool in output
    assert systems and "host policy may withhold private" in systems[0]
    assert tool not in systems[0]
    assert "- %s:" % tool not in prompts[0]


def test_hosted_claim_review_cannot_bypass_local_only_policy(monkeypatch):
    responses = [
        '{"final":"The requested symbol was not found."}',
        '{"final":"blocked"}',
    ]
    prompts = []
    dispatches = []
    generate = _hosted_generate(responses, prompts)

    monkeypatch.setattr(
        server, "_serve_target",
        lambda *args, **kwargs: (
            "glm-5.2:cloud", True, False, "cloud-general"
        ),
    )
    monkeypatch.setattr(server, "_make_generate", lambda *args, **kwargs: generate)
    monkeypatch.setattr(
        server, "_agent_negative_claim_review",
        lambda *args, **kwargs: {
            "decision": "continue",
            "reason": "verify the negative",
            "tool": "text_search",
            "args": {"query": "private marker", "root": "."},
        },
    )
    monkeypatch.setattr(
        server, "_agent_dispatch_observed",
        lambda *args, **kwargs: dispatches.append((args, kwargs)) or "TOP_SECRET",
    )

    server._agent_impl(
        "decide whether a requested symbol exists",
        tier="cloud-general",
        max_steps=2,
        include_evidence=True,
    )

    assert not dispatches
    assert len(prompts) == 2
    assert "local-only tool 'text_search'" in prompts[1]
    assert "TOP_SECRET" not in prompts[1]


def test_hosted_tool_manifest_does_not_readvertise_local_only_tools(monkeypatch):
    responses = [
        '{"tool":"tool_manifest","args":{}}',
        '{"final":"done"}',
    ]
    prompts = []
    dispatches = []
    generate = _hosted_generate(responses, prompts)
    monkeypatch.setattr(
        server, "_serve_target",
        lambda *args, **kwargs: (
            "glm-5.2:cloud", True, False, "cloud-general"
        ),
    )
    monkeypatch.setattr(server, "_make_generate", lambda *args, **kwargs: generate)
    monkeypatch.setattr(
        server, "_agent_dispatch_observed",
        lambda *args, **kwargs: dispatches.append((args, kwargs)) or "unsafe",
    )

    server._agent_impl(
        "show available tools", tier="cloud-general", max_steps=2,
        include_evidence=True,
    )

    assert not dispatches
    assert len(prompts) == 2
    for name in capabilities.CAPABILITIES:
        assert "- %s:" % name not in prompts[1]


def test_local_agent_keeps_host_brief_and_all_twelve_tools(monkeypatch):
    systems = []

    def generate(prompt, history=None):
        return '{"final":"done"}'

    generate.last_usage = {}
    generate.last_response_meta = {}
    generate.num_predict_override = None
    monkeypatch.setattr(
        server, "_serve_target",
        lambda *args, **kwargs: ("sonder:latest", False, True, "code"),
    )
    monkeypatch.setattr(server.environment_probe, "agent_brief", lambda: "HOST-BRIEF")
    monkeypatch.setattr(
        server, "_make_generate",
        lambda model, system, *args, **kwargs: systems.append(system) or generate,
    )

    assert server._agent_impl("summarize", max_steps=1) == "done"
    assert systems and "HOST-BRIEF" in systems[0]
    for name in capabilities.CAPABILITIES:
        assert "- %s:" % name in server._agent_tool_help()


def test_local_read_only_project_dedup_and_autopilot_sets_are_unchanged():
    names = set(capabilities.CAPABILITIES)
    rootless = {"environment_status", "hardware_profile", "file_policy"}
    non_work = {"environment_status", "hardware_profile"}
    assert names <= server.REPOSITORY_READ_ONLY_TOOLS
    assert names <= server._PROJECT_BOUND_AGENT_TOOLS
    assert names - rootless <= server._PROJECT_SCOPED_PATH_TOOLS
    assert rootless.isdisjoint(server._PROJECT_SCOPED_PATH_TOOLS)
    assert names <= server._AGENT_DEDUPLICATED_INSPECTION_TOOLS
    assert names - non_work <= server._WORK_INSPECTION_TOOLS
    assert non_work.isdisjoint(server._WORK_INSPECTION_TOOLS)
    assert names - non_work <= server._AUTOPILOT_OBSERVE_TOOLS
    assert non_work.isdisjoint(server._AUTOPILOT_OBSERVE_TOOLS)
    assert names.isdisjoint(server._WORK_MUTATION_TOOLS)


def test_tool_capability_module_keeps_live_reload_identity(monkeypatch):
    original = server.tool_capabilities
    replacement = object()
    monkeypatch.setattr(
        server.live_reload,
        "reload_changed_modules",
        lambda names: {"tool_capabilities": replacement},
    )
    try:
        server._maybe_live_reload()
        assert server.tool_capabilities is replacement
        assert "tool_capabilities" in server.LIVE_RELOAD_MODULES
    finally:
        server.tool_capabilities = original


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
