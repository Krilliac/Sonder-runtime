"""admin_status reports the whole hosted/admin safety state, not just an account count."""
import sqlite3

import server


def test_admin_status_reports_every_safety_line(monkeypatch):
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server, "_admin_account_from_token", lambda _token: None)
    monkeypatch.setattr(server, "_open_db", lambda: sqlite3.connect(":memory:"))
    monkeypatch.setattr(server.admin_auth, "account_count", lambda _conn: 3)
    monkeypatch.delenv("SONDER_API_KEY", raising=False)
    monkeypatch.setenv("SONDER_REQUIRE_ACCOUNT", "1")
    lines = server.admin_status("").splitlines()
    assert lines[0] == "sonder admin status"
    assert "  accounts: 3" in lines
    assert "  auth mode: local-open" in lines
    assert "  require account: 1" in lines
    assert any(line.startswith("  hosted/cloud allowed: ") for line in lines)
    assert "  logged in: no" in lines
    assert any(line.startswith("  safeguards: ") for line in lines)
