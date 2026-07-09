import command_registry


def test_command_registry_filters_by_risk_and_category():
    dangerous = command_registry.format_commands("dangerous")
    assert "/delete" in dangerous
    assert "filesystem" in command_registry.format_commands("filesystem")


def test_command_registry_handles_no_matches():
    out = command_registry.format_commands("definitely-not-a-command")
    assert "(no matching commands)" in out
