"""Runtime stash rendering lives in the domain; the root name is an alias."""
import server
from sonder_runtime.domain.updates import stash_formatting


def test_root_helper_is_an_identity_preserving_alias():
    assert server._runtime_stash_format is stash_formatting.format_stash


def test_status_renders_counts_and_commands_without_paths():
    data = {"clean": False, "change_count": 3, "stash_count": 1, "paths": ["secret.env"]}
    text = stash_formatting.format_stash(data)
    assert text.splitlines() == [
        "Sonder source recovery stash:",
        "  checkout: dirty",
        "  changes: 3",
        "  recovery stashes: 1",
        "  commands: /stash save | /stash save-untracked | /stash pop",
    ]
    assert "secret.env" not in text
    assert "  checkout: clean" in stash_formatting.format_stash({"clean": True})


def test_save_and_pop_report_only_the_resulting_checkout_state():
    assert stash_formatting.format_stash({"changed": False}, action="save") == (
        "runtime source stash: checkout already clean; no stash created"
    )
    assert stash_formatting.format_stash({"changed": True, "after": {"clean": True}}, action="save") == (
        "runtime source stash: saved changes; checkout is now clean"
    )
    assert stash_formatting.format_stash(
        {"changed": True, "after": {"clean": False}}, action="save-untracked",
    ) == "runtime source stash: saved changes; checkout is now not clean"
    assert stash_formatting.format_stash({"changed": True, "after": {"clean": False}}, action="pop") == (
        "runtime source stash: restored top recovery stash; checkout is now dirty"
    )
