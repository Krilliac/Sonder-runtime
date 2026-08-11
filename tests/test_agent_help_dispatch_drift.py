"""Drift guards: never advertise a tool to an agent that it cannot call.

Sonder describes its model-callable tools on several parallel surfaces -- the
full-agent help block, the repository read-only help block, the hosted (cloud)
variants of both, and the ``REPOSITORY_READ_ONLY_TOOLS`` allow-list -- while
``_agent_dispatch`` and ``_cloud_agent_tool_policy_error`` decide what actually
runs.  Nothing asserted that the advertising surfaces are a subset of the
deciding ones, so they drifted apart independently: a hosted agent was told it
could nest a model call that host policy hard-denies, and the repository help
and allow-list agreed perfectly with each other while both had drifted from
dispatch.

These tests therefore *discover* the surfaces rather than listing them, so a
sixth surface -- a new ``*_TOOL_HELP`` constant, or a new boolean flag on
``_agent_tool_help`` -- is covered the day it is introduced.  They also guard
their own parser: a help-format change that stopped matching would otherwise
turn every subset assertion below into a tautology over the empty set.
"""
from __future__ import annotations

import inspect
import itertools

import server
import tool_capabilities as capabilities


_HELP_ENTRY_PREFIX = "- "

# Floors, not expected values.  Their only job is to make a silently-broken
# parser or an empty dispatcher fail loudly instead of satisfying every
# "advertised is a subset of dispatchable" assertion vacuously.
_MIN_ADVERTISED_PER_SURFACE = 30
_MIN_DISPATCH_BRANCHES = 90

# Anchor advertised by every surface (local-only tools are stripped from the
# hosted surfaces, so the anchor must not be one of those).
_UNIVERSAL_ANCHOR = "memory_search"

# Pre-existing gap on the full (non-read-only) agent help block: execution,
# dependency, VCS and refactor tools that are advertised but still have no
# ``_agent_dispatch`` branch.  This allowance may only ever shrink, and the
# test below proves it is disjoint from every read-only surface so it can
# never be used to hide a repository-agent or hosted-agent regression.
_KNOWN_UNDISPATCHABLE_HELP_ENTRIES = frozenset({
    "apply_patch", "build_clean", "build_run",
    "dependency_add", "dependency_audit", "dependency_remove",
    "dependency_update", "diff_files", "find_references", "format_code",
    "git_branch", "git_checkout", "git_cherry_pick", "git_commit",
    "git_merge", "git_stash", "git_tag", "lint_run", "rename_symbol",
    "secret_scan", "test_run", "typecheck_run",
})


def _advertised_tools(help_text):
    """Tool names an agent help block advertises as callable."""
    names = set()
    for line in help_text.splitlines():
        stripped = line.lstrip()
        if not stripped.startswith(_HELP_ENTRY_PREFIX):
            continue
        name, separator, _ = stripped[len(_HELP_ENTRY_PREFIX):].partition(":")
        name = name.strip()
        if separator and name.isidentifier():
            names.add(name)
    return frozenset(names)


def _help_flag_names():
    """Boolean switches ``_agent_tool_help`` selects a surface with."""
    return tuple(
        name
        for name, parameter in inspect.signature(
            server._agent_tool_help
        ).parameters.items()
        if isinstance(parameter.default, bool)
    )


def _agent_help_surfaces():
    """Every agent-facing help surface, discovered instead of hardcoded.

    Two discovery rules keep future surfaces in scope automatically: any
    module-level ``*_TOOL_HELP`` constant, and every combination of
    ``_agent_tool_help``'s boolean flags -- so adding a flag doubles the
    covered surfaces rather than silently adding an unchecked one.
    """
    surfaces = {}
    for name, value in sorted(vars(server).items()):
        if name.endswith("_TOOL_HELP") and isinstance(value, str):
            surfaces[name] = ({}, value)
    flags = _help_flag_names()
    for combination in itertools.product((False, True), repeat=len(flags)):
        keywords = dict(zip(flags, combination))
        label = "_agent_tool_help(%s)" % ", ".join(
            "%s=%s" % item for item in sorted(keywords.items())
        )
        surfaces[label] = (keywords, server._agent_tool_help(**keywords))
    return surfaces


def test_help_surface_discovery_and_parsing_cannot_go_vacuous():
    surfaces = _agent_help_surfaces()
    assert {"AGENT_TOOL_HELP", "REPOSITORY_AGENT_TOOL_HELP"} <= set(surfaces)
    assert len(_help_flag_names()) >= 2
    assert len(surfaces) >= 2 + 2 ** len(_help_flag_names())
    for label, (_, text) in sorted(surfaces.items()):
        advertised = _advertised_tools(text)
        assert len(advertised) >= _MIN_ADVERTISED_PER_SURFACE, label
        assert _UNIVERSAL_ANCHOR in advertised, label
        # The parser must still see a newly advertised entry; if a help-format
        # change made it blind, every subset assertion here would pass on an
        # empty set and assert nothing at all.
        probe = "%s\n- __drift_probe__: {}" % text
        assert "__drift_probe__" in _advertised_tools(probe), label
    dispatch = capabilities.dispatch_names(server._agent_dispatch)
    assert len(dispatch) >= _MIN_DISPATCH_BRANCHES


def test_no_help_surface_advertises_a_tool_its_own_host_policy_denies():
    for label, (keywords, text) in sorted(_agent_help_surfaces().items()):
        if not keywords.get("cloud"):
            continue
        denied = sorted(
            name for name in _advertised_tools(text)
            if server._cloud_agent_tool_policy_error(
                name, unsafe=bool(keywords.get("unsafe"))
            )
        )
        assert denied == [], "%s advertises hosted-denied tools: %s" % (
            label, denied,
        )


def test_hosted_help_tracks_policy_in_both_directions():
    """The hosted filter must be derived from policy, not a hardcoded deletion."""
    full = _advertised_tools(server.AGENT_TOOL_HELP)
    hosted = _advertised_tools(server._agent_tool_help(cloud=True))
    hosted_unsafe = _advertised_tools(
        server._agent_tool_help(cloud=True, unsafe=True)
    )
    nested = frozenset(server._CLOUD_AGENT_NESTED_MODEL_TOOLS)
    local_only = frozenset(server._CLOUD_AGENT_LOCAL_ONLY_TOOLS)

    # A local agent may still nest a model call and read the host.
    assert nested <= full
    assert local_only <= full
    # A hosted agent may do neither, so it must be told about neither.
    assert nested.isdisjoint(hosted)
    assert local_only.isdisjoint(hosted)
    # Unsafe lab mode re-allows nested model calls, so it re-advertises exactly
    # those -- and still never re-advertises host-data tools.
    assert nested & full <= hosted_unsafe
    assert local_only.isdisjoint(hosted_unsafe)


def test_repository_help_allowlist_and_dispatch_all_agree():
    """The read-only trio drifted together; assert all three, not two of three."""
    dispatch = capabilities.dispatch_names(server._agent_dispatch)
    advertised = _advertised_tools(server.REPOSITORY_AGENT_TOOL_HELP)
    assert advertised == frozenset(server.REPOSITORY_READ_ONLY_TOOLS)
    assert sorted(advertised - dispatch) == []


def test_every_help_surface_only_advertises_dispatchable_tools():
    dispatch = capabilities.dispatch_names(server._agent_dispatch)
    for label, (_, text) in sorted(_agent_help_surfaces().items()):
        gap = sorted(
            _advertised_tools(text)
            - dispatch
            - _KNOWN_UNDISPATCHABLE_HELP_ENTRIES
        )
        assert gap == [], "%s advertises undispatchable tools: %s" % (label, gap)


def test_known_undispatchable_allowance_cannot_hide_a_read_only_regression():
    assert _KNOWN_UNDISPATCHABLE_HELP_ENTRIES.isdisjoint(
        server.REPOSITORY_READ_ONLY_TOOLS
    )
    assert _KNOWN_UNDISPATCHABLE_HELP_ENTRIES.isdisjoint(
        _advertised_tools(server.REPOSITORY_AGENT_TOOL_HELP)
    )
    # The allowance covers only "no dispatch branch yet".  Tools that dispatch
    # but are hard-denied to a hosted agent are a different defect and must
    # never be parked here.
    assert _KNOWN_UNDISPATCHABLE_HELP_ENTRIES.isdisjoint(
        server._CLOUD_AGENT_NESTED_MODEL_TOOLS
    )
