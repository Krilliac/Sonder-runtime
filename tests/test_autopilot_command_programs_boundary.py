"""Autopilot command program extraction lives in the domain; the root name is an alias."""
import json

import server
from sonder_runtime.domain.automation import command_programs


def test_root_helper_is_an_identity_preserving_alias():
    assert server._autopilot_command_programs is command_programs.command_programs


def test_programs_are_lowercased_basenames_of_each_command_head():
    extract = command_programs.command_programs
    assert extract(None) == []
    assert extract("") == []
    encoded = json.dumps([{"cmd": ["/usr/bin/Python3", "-m", "pytest"]}, ["MAKE", "test"]])
    assert extract(encoded) == ["python3", "make"]
    assert extract({"commands": [{"cmd": ["git", "status"]}]}) == ["git"]


def test_malformed_lists_are_marked_invalid_rather_than_guessed():
    extract = command_programs.command_programs
    assert extract("not json") == ["(invalid)"]
    assert extract(json.dumps({"commands": "x"})) == ["(invalid)"]
    assert extract(json.dumps([{"cmd": []}])) == ["(invalid)"]
    assert extract(json.dumps(["bare"])) == ["(invalid)"]
