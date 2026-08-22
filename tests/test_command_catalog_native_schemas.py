"""Native slash branches expose their positional usage in the catalog."""
from __future__ import annotations

from sonder_runtime.adapters.command_catalog import by_name


def test_artifactcheck_catalog_includes_required_path():
    command = by_name("/artifactcheck")
    assert command is not None
    assert command.usage() == "/artifactcheck <path> [recipe=auto]"
    assert command.params[0].required is True


def test_common_native_commands_are_not_advertised_as_bare_only():
    for name in ("/asset", "/weather", "/ensemble", "/work", "/agentcancel"):
        command = by_name(name)
        assert command is not None and command.params, name
