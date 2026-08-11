import command_registry


def test_command_registry_filters_by_risk_and_category():
    # The witness used to be "/delete", which is deliberately no longer graded
    # dangerous: the console branch hard-codes `dry_run=True`, so no console
    # spelling reaches a real delete, and grading it dangerous was the inverse
    # half of the #47 mis-grading (see tests/test_command_grading.py). This
    # assertion is about the *filter*, not about /delete, so it now takes a
    # witness that is genuinely dangerous -- and one the #47 fix is what makes
    # correct: /setaccount fronts admin_set_account, which is in _DANGEROUS.
    dangerous = command_registry.format_commands("dangerous")
    assert "/setaccount" in dangerous
    assert "/delete" not in dangerous
    assert "filesystem" in command_registry.format_commands("filesystem")


def test_command_registry_handles_no_matches():
    out = command_registry.format_commands("definitely-not-a-command")
    assert "(no matching commands)" in out


def test_command_registry_exposes_restart_safe_agent_controls():
    agents = command_registry.format_commands("agents")

    assert "/capacity" in agents
    assert "/agentcancel" in agents
    assert "/agentretry" in agents
