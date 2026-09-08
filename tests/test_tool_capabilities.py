import dataclasses
from dataclasses import replace
import inspect

import pytest

import server
import tool_capabilities as capabilities
from scripts import package_local_system as package


def test_initial_shadow_registry_is_immutable_and_has_no_drift():
    assert set(capabilities.CAPABILITIES) == {
        "environment_status", "toolchain_status", "hardware_profile", "file_policy",
        "workspace_inventory", "directory_tree", "file_find", "file_read",
        "file_read_range", "file_digest", "text_search", "repo_status",
        "repo_diff", "image_inspect", "artifact_risk_inspect", "process_list",
        "process_memory_risk_inspect",
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
    # CAPABILITIES intentionally describes only the initial inspection slice.
    # Lane control is separately described and must remain explicitly local.
    local_only |= {"agent_lane"}
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


@pytest.mark.parametrize("tool", sorted(set(capabilities.CAPABILITIES) | {"agent_lane"}))
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


def test_local_agent_keeps_host_brief_and_all_twelve_tools(monkeypatch, without_standing):
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

    assert without_standing(server._agent_impl("summarize", max_steps=1)) == "done"
    assert systems and "HOST-BRIEF" in systems[0]
    for name in capabilities.CAPABILITIES:
        assert "- %s:" % name in server._agent_tool_help()


def test_local_read_only_project_dedup_and_autopilot_sets_are_unchanged():
    names = set(capabilities.CAPABILITIES)
    process_tools = {"process_list", "process_memory_risk_inspect"}
    repository_names = names - process_tools
    rootless = {
        "environment_status", "toolchain_status", "hardware_profile", "file_policy",
        *process_tools,
    }
    non_work = {"environment_status", "hardware_profile"}
    assert repository_names <= server.REPOSITORY_READ_ONLY_TOOLS
    assert process_tools.isdisjoint(server.REPOSITORY_READ_ONLY_TOOLS)
    assert names <= server._PROJECT_BOUND_AGENT_TOOLS
    assert names - rootless <= server._PROJECT_SCOPED_PATH_TOOLS
    assert rootless.isdisjoint(server._PROJECT_SCOPED_PATH_TOOLS)
    assert names <= server._AGENT_DEDUPLICATED_INSPECTION_TOOLS
    assert names - non_work <= server._WORK_INSPECTION_TOOLS
    assert non_work.isdisjoint(server._WORK_INSPECTION_TOOLS)
    # process_list / process_memory_risk_inspect are workspace-policy only:
    # they sit outside REPOSITORY_READ_ONLY_TOOLS on purpose, and observe runs
    # execute with read_only=True, so listing them in the observe allowlist
    # advertised a call _repository_read_only_error always refused.
    assert names - non_work - process_tools <= server._AUTOPILOT_OBSERVE_TOOLS
    assert process_tools.isdisjoint(server._AUTOPILOT_OBSERVE_TOOLS)
    assert names - non_work <= server._AUTOPILOT_WORKSPACE_TOOLS
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
    # "ok" is exactly what this validator must never be able to say while it
    # inspects 16 of ~184 advertised tools.  The verdict names the coverage.
    assert report.startswith("partial: ")
    assert "17 of " in report
    assert "unvalidated" in report
    # Prove diagnostics consumes the shadow report without running its unrelated
    # model, database, NPU, and filesystem checks in this focused unit test.
    source = inspect.getsource(server.diagnostics)
    assert "tool_capability_shadow_report()" in source
    assert "tool capability shadow" in source


# --- the validator must state how much of the surface it validated ----------


def test_shadow_report_can_never_say_ok_while_the_surface_is_undescribed():
    """A validator that cannot say what fraction it validated is not a validator.

    Absence from _DESCRIPTORS silently exempts a tool from every drift check,
    so "no drift found" over 15 of ~184 advertised tools is a coverage
    statement wearing a verdict's clothes.  The verdict must carry the number.
    """
    surfaces = server._tool_capability_shadow_surfaces()
    report = capabilities.format_shadow_report(surfaces)

    assert not report.startswith("ok")
    described = len(capabilities.CAPABILITIES)
    advertised = len(
        set().union(*(names for _label, names in capabilities.advertised_surfaces(surfaces)))
    )
    assert advertised > described * 5, "the point of the number is that it is small"
    assert "%d of %d" % (described, advertised) in report
    assert "%d unvalidated" % (advertised - described) in report


def test_coverage_is_measured_for_every_field_of_the_snapshot():
    """Hand-listing the surfaces reproduces the defect one level up.

    A hardcoded list covers today's fields; a fifteenth would be silently
    unmeasured, exactly as a tool without a descriptor is silently unchecked.
    The list is therefore DERIVED from ShadowSurfaces, and this pins that --
    checking a handful of labels by name would not.
    """
    surfaces = server._tool_capability_shadow_surfaces()
    coverage = capabilities.shadow_coverage(surfaces)

    expected = [
        capabilities.surface_label(field.name)
        for field in dataclasses.fields(capabilities.ShadowSurfaces)
    ]
    assert [row.surface for row in coverage] == expected
    assert len(coverage) == len(dataclasses.fields(capabilities.ShadowSurfaces))

    by_surface = {row.surface: row for row in coverage}
    for row in coverage:
        assert row.described <= row.advertised
        assert row.unvalidated == row.advertised - row.described
    # Every surface is measured against the same 15 descriptors, so each is
    # mostly unvalidated -- and the report says so per surface.
    assert by_surface["direct-mcp"].unvalidated > 100
    assert by_surface["autopilot-workspace"].unvalidated > 50

    line = capabilities.format_coverage_report(surfaces)
    for row in coverage:
        assert "%s %d/%d" % (row.surface, row.described, row.advertised) in line


def test_a_text_surface_with_no_name_parser_fails_instead_of_being_skipped():
    """The one hand-maintained part must be loud when it goes stale."""
    surfaces = server._tool_capability_shadow_surfaces()
    parsers = dict(capabilities._TEXT_SURFACE_PARSERS)
    parsers.pop("tool_manifest")

    with pytest.raises(ValueError) as excinfo:
        capabilities.advertised_surfaces(surfaces, text_parsers=parsers)

    assert "tool_manifest" in str(excinfo.value)


def test_autopilot_transcript_sets_are_snapshotted_as_advertised_surfaces():
    """_AUTOPILOT_*_TOOLS are injected verbatim into the model transcript."""
    surfaces = server._tool_capability_shadow_surfaces()

    assert surfaces.autopilot_observe_tools == server._AUTOPILOT_OBSERVE_TOOLS
    assert surfaces.autopilot_workspace_tools == server._AUTOPILOT_WORKSPACE_TOOLS


def test_no_surface_can_be_omitted_from_a_snapshot():
    """A defaulted field is an unmeasured surface that reads as an empty one."""
    for field in dataclasses.fields(capabilities.ShadowSurfaces):
        assert field.default is dataclasses.MISSING, field.name
        assert field.default_factory is dataclasses.MISSING, field.name


def _empty_surfaces():
    return capabilities.ShadowSurfaces(**{
        field.name: ("" if field.type is str or "help" in field.name
                     or field.name == "tool_manifest" else frozenset())
        for field in dataclasses.fields(capabilities.ShadowSurfaces)
    })


def test_measuring_nothing_is_unmeasured_not_a_clean_hundred_percent():
    """The A1 defect, one level up: 0 of 0 is not full coverage.

    A snapshot taken before registration advertises nothing, and a coverage
    report that answers "complete, 100.0%" to that has done precisely what
    "ok (15 descriptors)" did -- reported the absence of evidence as evidence
    of absence.
    """
    report = capabilities.format_shadow_report(_empty_surfaces())

    assert report.startswith("unmeasured")
    assert "complete" not in report
    assert "100.0%" not in report


def test_a_surface_that_advertises_nothing_has_no_coverage_fraction():
    assert capabilities.SurfaceCoverage("x", 0, 0).described_fraction is None
    assert capabilities.SurfaceCoverage("x", 4, 1).described_fraction == 0.25


def test_diagnostics_reports_shadow_coverage_per_surface():
    source = inspect.getsource(server.diagnostics)
    assert "tool_capability_coverage_report()" in source
    assert "tool capability coverage" in source
    line = server.tool_capability_coverage_report()
    assert "direct-mcp 17/" in line
