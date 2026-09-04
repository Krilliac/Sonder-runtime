"""Tests for declarative tool governance (policy-as-code)."""
from __future__ import annotations

import pytest

from sonder_runtime.domain.tools.governance import (
    GovernanceInputError,
    PolicyEngine,
    ToolPolicy,
    Verdict,
)


# ------------------------------------------------------------------
# Verdict basics
# ------------------------------------------------------------------


class TestVerdictEnum:
    def test_values(self):
        assert Verdict.ALLOW == "allow"
        assert Verdict.DENY == "deny"
        assert Verdict.REQUIRE_APPROVAL == "require_approval"

    def test_construction_from_string(self):
        assert Verdict("allow") is Verdict.ALLOW
        assert Verdict("deny") is Verdict.DENY
        assert Verdict("require_approval") is Verdict.REQUIRE_APPROVAL


# ------------------------------------------------------------------
# ToolPolicy construction and matching
# ------------------------------------------------------------------


class TestToolPolicy:
    def test_basic_construction(self):
        policy = ToolPolicy(
            name="read-tools",
            tools=("file.read", "file.list"),
            verdict=Verdict.ALLOW,
        )
        assert policy.name == "read-tools"
        assert policy.tools == ("file.read", "file.list")
        assert policy.verdict is Verdict.ALLOW
        assert policy.priority == 0

    def test_matches_exact_tool(self):
        policy = ToolPolicy(name="p", tools=("file.read",), verdict=Verdict.ALLOW)
        assert policy.matches_tool("file.read") is True
        assert policy.matches_tool("file.write") is False

    def test_matches_glob_star(self):
        policy = ToolPolicy(name="p", tools=("file.*",), verdict=Verdict.ALLOW)
        assert policy.matches_tool("file.read") is True
        assert policy.matches_tool("file.write") is True
        assert policy.matches_tool("network.get") is False

    def test_matches_wildcard_all(self):
        policy = ToolPolicy(name="p", tools=("*",), verdict=Verdict.DENY)
        assert policy.matches_tool("anything") is True
        assert policy.matches_tool("file.read") is True

    def test_matches_multiple_patterns(self):
        policy = ToolPolicy(
            name="p",
            tools=("network.*", "http.*"),
            verdict=Verdict.DENY,
        )
        assert policy.matches_tool("network.get") is True
        assert policy.matches_tool("http.post") is True
        assert policy.matches_tool("file.read") is False

    def test_conditions_path_prefix(self):
        policy = ToolPolicy(
            name="p",
            tools=("file.write",),
            verdict=Verdict.REQUIRE_APPROVAL,
            conditions={"path_prefix": "/workspace/"},
        )
        assert policy.matches_conditions({"path": "/workspace/foo.txt"}) is True
        assert policy.matches_conditions({"path": "/etc/passwd"}) is False
        assert policy.matches_conditions(None) is False
        assert policy.matches_conditions({}) is False

    def test_conditions_arbitrary_key(self):
        policy = ToolPolicy(
            name="p",
            tools=("deploy.*",),
            verdict=Verdict.REQUIRE_APPROVAL,
            conditions={"environment": "production"},
        )
        assert policy.matches_conditions({"environment": "production"}) is True
        assert policy.matches_conditions({"environment": "staging"}) is False

    def test_no_conditions_always_matches(self):
        policy = ToolPolicy(name="p", tools=("file.read",), verdict=Verdict.ALLOW)
        assert policy.matches_conditions(None) is True
        assert policy.matches_conditions({}) is True
        assert policy.matches_conditions({"anything": "value"}) is True

    def test_empty_name_raises(self):
        with pytest.raises(GovernanceInputError, match="name is required"):
            ToolPolicy(name="", tools=("x",), verdict=Verdict.ALLOW)

    def test_empty_tools_raises(self):
        with pytest.raises(GovernanceInputError, match="must specify at least one tool"):
            ToolPolicy(name="p", tools=(), verdict=Verdict.ALLOW)

    def test_bad_priority_raises(self):
        with pytest.raises(GovernanceInputError, match="priority must be an integer"):
            ToolPolicy(name="p", tools=("x",), verdict=Verdict.ALLOW, priority="high")  # type: ignore[arg-type]

    def test_verdict_from_raw_string(self):
        policy = ToolPolicy(name="p", tools=("x",), verdict="allow")  # type: ignore[arg-type]
        assert policy.verdict is Verdict.ALLOW

    def test_invalid_verdict_raises(self):
        with pytest.raises(GovernanceInputError, match="invalid verdict"):
            ToolPolicy(name="p", tools=("x",), verdict="maybe")  # type: ignore[arg-type]


# ------------------------------------------------------------------
# PolicyEngine evaluation
# ------------------------------------------------------------------


class TestPolicyEngine:
    def test_single_allow(self):
        engine = PolicyEngine([
            ToolPolicy(name="allow-read", tools=("file.read",), verdict=Verdict.ALLOW),
        ])
        verdict, reason = engine.evaluate("file.read")
        assert verdict is Verdict.ALLOW

    def test_single_deny(self):
        engine = PolicyEngine([
            ToolPolicy(
                name="deny-network",
                tools=("network.*",),
                verdict=Verdict.DENY,
                reason="Network access blocked",
            ),
        ])
        verdict, reason = engine.evaluate("network.get")
        assert verdict is Verdict.DENY
        assert reason == "Network access blocked"

    def test_require_approval(self):
        engine = PolicyEngine([
            ToolPolicy(
                name="approve-writes",
                tools=("file.write",),
                verdict=Verdict.REQUIRE_APPROVAL,
                reason="Writes need approval",
            ),
        ])
        verdict, reason = engine.evaluate("file.write")
        assert verdict is Verdict.REQUIRE_APPROVAL
        assert reason == "Writes need approval"

    def test_default_deny_when_no_match(self):
        engine = PolicyEngine([
            ToolPolicy(name="allow-read", tools=("file.read",), verdict=Verdict.ALLOW),
        ])
        verdict, reason = engine.evaluate("network.get")
        assert verdict is Verdict.DENY
        assert "no policy matched" in reason

    def test_priority_ordering_higher_wins(self):
        engine = PolicyEngine([
            ToolPolicy(
                name="deny-all",
                tools=("*",),
                verdict=Verdict.DENY,
                reason="default deny",
                priority=0,
            ),
            ToolPolicy(
                name="allow-read",
                tools=("file.read",),
                verdict=Verdict.ALLOW,
                reason="reads are safe",
                priority=10,
            ),
        ])
        verdict, reason = engine.evaluate("file.read")
        assert verdict is Verdict.ALLOW
        assert reason == "reads are safe"

        # A tool not covered by the higher-priority rule falls through.
        verdict2, reason2 = engine.evaluate("network.get")
        assert verdict2 is Verdict.DENY
        assert reason2 == "default deny"

    def test_priority_tie_broken_by_name(self):
        engine = PolicyEngine([
            ToolPolicy(name="b-allow", tools=("file.read",), verdict=Verdict.ALLOW, priority=5),
            ToolPolicy(name="a-deny", tools=("file.*",), verdict=Verdict.DENY, priority=5),
        ])
        # Same priority: sorted by name ascending, "a-deny" comes first.
        verdict, _ = engine.evaluate("file.read")
        assert verdict is Verdict.DENY

    def test_conditions_filter_match(self):
        engine = PolicyEngine([
            ToolPolicy(
                name="approve-workspace-writes",
                tools=("file.write",),
                verdict=Verdict.REQUIRE_APPROVAL,
                conditions={"path_prefix": "/workspace/"},
                reason="workspace writes need approval",
                priority=10,
            ),
            ToolPolicy(
                name="deny-writes",
                tools=("file.write",),
                verdict=Verdict.DENY,
                reason="writes denied by default",
                priority=0,
            ),
        ])
        # Context matches the condition -- require_approval wins.
        verdict, reason = engine.evaluate("file.write", {"path": "/workspace/foo.py"})
        assert verdict is Verdict.REQUIRE_APPROVAL

        # Context does not match the condition -- falls through to deny.
        verdict2, reason2 = engine.evaluate("file.write", {"path": "/etc/shadow"})
        assert verdict2 is Verdict.DENY
        assert reason2 == "writes denied by default"

        # No context -- condition not satisfied, falls through.
        verdict3, _ = engine.evaluate("file.write")
        assert verdict3 is Verdict.DENY

    def test_empty_engine_denies_everything(self):
        engine = PolicyEngine([])
        verdict, reason = engine.evaluate("anything")
        assert verdict is Verdict.DENY

    def test_reason_auto_generated_when_empty(self):
        engine = PolicyEngine([
            ToolPolicy(name="quiet-rule", tools=("x",), verdict=Verdict.ALLOW),
        ])
        _, reason = engine.evaluate("x")
        assert "quiet-rule" in reason

    def test_non_policy_entry_raises(self):
        with pytest.raises(GovernanceInputError, match="ToolPolicy"):
            PolicyEngine([{"name": "bad"}])  # type: ignore[list-item]


# ------------------------------------------------------------------
# YAML loading
# ------------------------------------------------------------------


_SAMPLE_YAML = """\
policies:
  - name: allow-read-tools
    tools: ["file.read", "file.list", "file.search"]
    verdict: allow
    priority: 10

  - name: deny-network-by-default
    tools: ["network.*", "http.*"]
    verdict: deny
    reason: "Network access requires explicit approval"

  - name: approve-file-writes
    tools: ["file.write", "file.delete"]
    verdict: require_approval
    conditions:
      path_prefix: "/workspace/"
    reason: "File modifications need human approval"
"""


class TestYAMLLoading:
    def test_load_yaml_string(self):
        engine = PolicyEngine.load_yaml_string(_SAMPLE_YAML)
        assert len(engine.policies) == 3

    def test_loaded_policies_evaluate_correctly(self):
        engine = PolicyEngine.load_yaml_string(_SAMPLE_YAML)

        # Allowed read.
        verdict, _ = engine.evaluate("file.read")
        assert verdict is Verdict.ALLOW

        # Denied network.
        verdict, reason = engine.evaluate("http.post")
        assert verdict is Verdict.DENY
        assert "Network access" in reason

        # Require approval for workspace writes.
        verdict, reason = engine.evaluate("file.write", {"path": "/workspace/a.txt"})
        assert verdict is Verdict.REQUIRE_APPROVAL

        # Outside workspace -- no condition match, default deny.
        verdict, _ = engine.evaluate("file.write", {"path": "/tmp/a.txt"})
        assert verdict is Verdict.DENY

    def test_load_single_tool_string(self):
        engine = PolicyEngine.load_yaml_string("""\
policies:
  - name: deny-exec
    tools: "shell.exec"
    verdict: deny
""")
        verdict, _ = engine.evaluate("shell.exec")
        assert verdict is Verdict.DENY

    def test_missing_policies_key_raises(self):
        with pytest.raises(GovernanceInputError, match="top-level"):
            PolicyEngine.load_yaml_string("rules: []")

    def test_policies_not_list_raises(self):
        with pytest.raises(GovernanceInputError, match="must be a list"):
            PolicyEngine.load_yaml_string("policies: not-a-list")

    def test_policy_entry_not_mapping_raises(self):
        with pytest.raises(GovernanceInputError, match="must be a mapping"):
            PolicyEngine.load_yaml_string("policies:\n  - just-a-string")


# ------------------------------------------------------------------
# Multiple matching policies resolved by priority
# ------------------------------------------------------------------


class TestMultiplePolicies:
    def test_specific_override_beats_catchall(self):
        engine = PolicyEngine([
            ToolPolicy(name="catchall-deny", tools=("*",), verdict=Verdict.DENY, priority=0),
            ToolPolicy(
                name="allow-safe-reads",
                tools=("file.read", "file.list"),
                verdict=Verdict.ALLOW,
                priority=10,
            ),
            ToolPolicy(
                name="approve-dangerous-writes",
                tools=("file.write", "file.delete"),
                verdict=Verdict.REQUIRE_APPROVAL,
                priority=5,
            ),
        ])
        assert engine.evaluate("file.read")[0] is Verdict.ALLOW
        assert engine.evaluate("file.list")[0] is Verdict.ALLOW
        assert engine.evaluate("file.write")[0] is Verdict.REQUIRE_APPROVAL
        assert engine.evaluate("file.delete")[0] is Verdict.REQUIRE_APPROVAL
        assert engine.evaluate("network.get")[0] is Verdict.DENY

    def test_layered_glob_policies(self):
        engine = PolicyEngine([
            ToolPolicy(name="deny-all-network", tools=("network.*",), verdict=Verdict.DENY, priority=0),
            ToolPolicy(
                name="allow-internal-network",
                tools=("network.internal",),
                verdict=Verdict.ALLOW,
                priority=10,
            ),
        ])
        assert engine.evaluate("network.internal")[0] is Verdict.ALLOW
        assert engine.evaluate("network.external")[0] is Verdict.DENY
