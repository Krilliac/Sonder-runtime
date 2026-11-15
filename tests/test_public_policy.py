"""Role tool policy: the pure domain rule and the shipped allowlist data."""

from __future__ import annotations

import json

import pytest

from sonder_runtime.adapters.security import role_tool_policy
from sonder_runtime.domain import public_policy


def _doc(*entries):
    return {"schema_version": 1, "tools": list(entries)}


def _entry(name="status", why="read-only"):
    return {"name": name, "why": why}


def test_unrestricted_roles_are_allowed_every_tool():
    for role in (public_policy.ROLE_DEVELOPER, public_policy.ROLE_ADMIN):
        assert public_policy.decide_tool("file_write", role, frozenset()) == public_policy.ToolAllowed()


@pytest.mark.parametrize("role", ("user", "", "guest", "Developer", None))
def test_user_and_unknown_roles_are_denied_by_default(role):
    decision = public_policy.decide_tool("file_write", role, frozenset({"status"}))
    assert isinstance(decision, public_policy.ToolRefused)
    assert "file_write" in decision.reason


def test_allowlisted_tool_is_allowed_for_user():
    allowed = frozenset({"status"})
    assert public_policy.decide_tool("status", "user", allowed) == public_policy.ToolAllowed()
    assert isinstance(public_policy.decide_tool("status ", "user", allowed), public_policy.ToolRefused)


@pytest.mark.parametrize("payload", (
    [], {}, {"schema_version": 2, "tools": []}, {"schema_version": 1},
    _doc("status"), _doc({"name": "status"}), _doc({"name": "", "why": "x"}),
    _doc({"name": " status", "why": "x"}), _doc(_entry(), _entry()),
    _doc({"name": 3, "why": "x"}),
))
def test_parse_allowlist_rejects_malformed_documents(payload):
    with pytest.raises(ValueError):
        public_policy.parse_allowlist(payload)


def test_parse_allowlist_accepts_a_justified_list():
    assert public_policy.parse_allowlist(_doc(_entry("a"), _entry("b"))) == frozenset({"a", "b"})


def test_shipped_allowlist_is_valid_and_excludes_host_acting_tools():
    allowed = role_tool_policy.user_tool_allowlist()
    assert allowed, "shipped allowlist must load"
    for tool in ("file_write", "run_code", "script_run", "ui_action", "git_commit", "loop",
                 "tool_inventory", "web_search", "web_fetch"):
        assert tool not in allowed, tool


def test_unreadable_allowlist_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(role_tool_policy, "ALLOWLIST_PATH", tmp_path / "missing.json")
    role_tool_policy.user_tool_allowlist.cache_clear()
    try:
        assert role_tool_policy.user_tool_allowlist() == frozenset()
        assert role_tool_policy.refusal_reason("status", "user")
        (tmp_path / "bad.json").write_text(json.dumps({"schema_version": 9}), encoding="utf-8")
        monkeypatch.setattr(role_tool_policy, "ALLOWLIST_PATH", tmp_path / "bad.json")
        role_tool_policy.user_tool_allowlist.cache_clear()
        assert role_tool_policy.user_tool_allowlist() == frozenset()
    finally:
        monkeypatch.undo()
        role_tool_policy.user_tool_allowlist.cache_clear()
