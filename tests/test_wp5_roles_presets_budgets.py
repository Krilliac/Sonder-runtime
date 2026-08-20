from sonder_runtime.application.agents.presets import builtin_presets, resolve_preset
from sonder_runtime.domain.agents.roles import AgentRole, BudgetLimit, role_budget


def test_roles_have_positive_deterministic_budgets():
    assert {item.role for item in builtin_presets()} == {
        AgentRole.EXPLORER, AgentRole.EDITOR, AgentRole.ARCHITECT,
        AgentRole.REVIEWER, AgentRole.VERIFIER,
    }
    assert role_budget(AgentRole.ARCHITECT) == role_budget(AgentRole.ARCHITECT)


def test_preset_resolution_is_case_insensitive_and_parent_bounded():
    assert resolve_preset(" CODE ").role is AgentRole.EDITOR
    assert resolve_preset("code", max_budget=BudgetLimit(steps=20, output_tokens=6000, wall_seconds=600))


def test_preset_cannot_exceed_parent_budget():
    try:
        resolve_preset("code", max_budget=BudgetLimit(steps=1))
    except ValueError as exc:
        assert "steps" in str(exc)
    else:
        raise AssertionError("expected parent budget rejection")
