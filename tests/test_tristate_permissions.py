"""Permission gates consume tri-state tool metadata conservatively."""

import permission_modes as pm
import pytest

from sonder_runtime.domain.tools.traits import ToolTraits, TriState


def no_rule(_name):
    return None


def test_external_read_only_hint_never_relaxes_permission(monkeypatch):
    monkeypatch.setattr(pm, "_catalog_risk_of", lambda _name: "ask")
    advisory_read = ToolTraits(
        read_only=TriState.TRUE,
        destructive=TriState.FALSE,
        host_declared=False,
    )
    decision = pm.decide(
        "external_tool", mode=pm.AUTO, interactive=False,
        rule_lookup=no_rule, traits=advisory_read,
    )
    assert decision.risk == "dangerous"
    assert decision.action == pm.DENY


def test_host_declared_builtin_read_only_remains_safe(monkeypatch):
    monkeypatch.setattr(pm, "_catalog_risk_of", lambda _name: "ask")
    decision = pm.decide(
        "file_read", mode=pm.PLAN, rule_lookup=no_rule,
    )
    assert decision.risk == "safe"
    assert decision.action == pm.ALLOW


def test_external_unknown_traits_are_conservative(monkeypatch):
    monkeypatch.setattr(pm, "_catalog_risk_of", lambda _name: "ask")
    decision = pm.decide(
        "ordinary_tool", mode=pm.AUTO, interactive=False,
        rule_lookup=no_rule, traits=ToolTraits(host_declared=False),
    )
    assert decision.risk == "dangerous"
    assert decision.action == pm.DENY


def test_metadata_cannot_lower_dangerous_catalog_risk(monkeypatch):
    monkeypatch.setattr(pm, "_catalog_risk_of", lambda _name: "dangerous")
    read_hint = ToolTraits(
        read_only=TriState.TRUE,
        destructive=TriState.FALSE,
    )
    decision = pm.decide(
        "file_delete", mode=pm.AUTO, rule_lookup=no_rule, traits=read_hint,
    )
    assert decision.risk == "dangerous"
    assert decision.action == pm.ASK


def test_explicit_deny_still_outranks_traits():
    read_hint = ToolTraits(
        read_only=TriState.TRUE,
        destructive=TriState.FALSE,
    )
    decision = pm.decide(
        "file_read", mode=pm.AUTO,
        rule_lookup=lambda _name: {"action": pm.DENY, "pattern": "file_read"},
        traits=read_hint,
    )
    assert decision.action == pm.DENY
    assert decision.source == "rule"


def test_unknown_tool_stays_unclassified_fail_closed():
    read_hint = ToolTraits(
        read_only=TriState.TRUE,
        destructive=TriState.FALSE,
    )
    decision = pm.decide(
        "not_registered", mode=pm.AUTO, interactive=False,
        rule_lookup=no_rule, traits=read_hint,
    )
    assert decision.risk == pm.UNCLASSIFIED
    assert decision.action == pm.DENY


def test_destructive_false_does_not_lower_execution_risk(monkeypatch):
    monkeypatch.setattr(pm, "_catalog_risk_of", lambda _name: "execution")
    metadata = ToolTraits(
        read_only=TriState.FALSE,
        destructive=TriState.FALSE,
    )
    decision = pm.decide(
        "workspace_run", mode=pm.AUTO, rule_lookup=no_rule, traits=metadata,
    )
    assert decision.risk == "execution"
    assert decision.action == pm.ALLOW


def test_unknown_destructiveness_preserves_builtin_execution(monkeypatch):
    monkeypatch.setattr(pm, "_catalog_risk_of", lambda _name: "execution")
    decision = pm.decide(
        "workspace_run", mode=pm.AUTO, interactive=True, rule_lookup=no_rule,
        traits=ToolTraits(read_only=TriState.FALSE),
    )
    assert decision.risk == "execution"
    assert decision.action == pm.ALLOW


@pytest.mark.parametrize("risk", ["safe", "ask", "mutation", "execution"])
@pytest.mark.parametrize("traits", [ToolTraits(), ToolTraits(read_only=TriState.FALSE),
                                   ToolTraits(destructive=TriState.FALSE)])
def test_partial_host_traits_do_not_reclassify_builtin_risk(monkeypatch, risk, traits):
    """The 218 escalations came from treating missing metadata as a new grade."""
    monkeypatch.setattr(pm, "_catalog_risk_of", lambda _name: risk)
    assert pm.risk_of("builtin", traits=traits) == risk


@pytest.mark.parametrize("risk", ["safe", "ask", "mutation", "execution"])
def test_explicit_host_destructive_declaration_can_raise_builtin_risk(monkeypatch, risk):
    monkeypatch.setattr(pm, "_catalog_risk_of", lambda _name: risk)
    assert pm.risk_of("builtin", traits=ToolTraits(destructive=TriState.TRUE)) == "dangerous"
