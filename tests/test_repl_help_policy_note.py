"""``/help <cmd>`` names a standing deny rule that refuses the command.

``/delete`` is a hard-coded dry run, so the catalog grades it ``safe``; the
shipped ``file_delete`` deny rule still refuses it in every mode.  Help used
to advertise only the grade.  The deny rule is kept (an explicit deny
outranks every call site); help now says so.
"""
import pytest

import permission_modes
import server
import sonder_runtime.interfaces.repl.repl as sonder_repl


@pytest.fixture(autouse=True)
def _inject_legacy_runtime(monkeypatch):
    monkeypatch.setattr(sonder_repl, "_legacy_runtime", None)
    sonder_repl.configure_legacy_runtime(server)


def _shipped_rules(tool_name):
    if tool_name == "file_delete":
        return {"pattern": "file_delete", "action": "deny", "note": "destructive by default"}
    return None


def test_help_for_delete_names_the_rule_that_refuses_it(monkeypatch):
    monkeypatch.setattr(permission_modes, "_rule_lookup", _shipped_rules)

    note = sonder_repl._help_policy_note("delete")

    assert "policy:   refused" in note
    assert "pattern 'file_delete'" in note


def test_help_has_no_policy_note_without_a_deny_rule(monkeypatch):
    monkeypatch.setattr(permission_modes, "_rule_lookup", _shipped_rules)
    assert sonder_repl._help_policy_note("read") == ""
    assert sonder_repl._help_policy_note("") == ""

    monkeypatch.setattr(permission_modes, "_rule_lookup", lambda _tool: None)
    assert sonder_repl._help_policy_note("delete") == ""


def test_help_delete_and_the_gate_agree(monkeypatch, capsys):
    monkeypatch.setattr(permission_modes, "_rule_lookup", _shipped_rules)
    deleted = []
    monkeypatch.setattr(server, "file_delete", lambda **k: deleted.append(k) or "deleted")
    feed = iter(("/help delete", "/delete notes.txt", "/exit"))
    monkeypatch.setattr(sonder_repl, "_read_input", lambda *_a, **_k: next(feed))
    monkeypatch.setattr(sonder_repl, "_startup_banner", lambda *_args: "")
    monkeypatch.setattr(sonder_repl, "_maybe_live_reload", lambda: None)

    sonder_repl.main()

    out = capsys.readouterr().out
    assert "risk: safe" in out
    assert "policy:   refused" in out
    assert "refused /delete" in out
    # The deny rule is not relaxed for the dry-run call site.
    assert deleted == []
