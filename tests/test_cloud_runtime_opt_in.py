import server


def test_cloud_opt_in_is_process_local_and_revocable(monkeypatch):
    monkeypatch.delenv("SONDER_ALLOW_CLOUD", raising=False)
    monkeypatch.setattr(server, "_refresh_live_cloud_tiers", lambda: None)

    assert "disabled" in server.cloud_opt_in("status")
    enabled = server.cloud_opt_in("on")
    assert "ENABLED" in enabled
    assert "leave this machine" in enabled
    assert server.cloud_allowed() is True

    assert "disabled" in server.cloud_opt_in("off")
    assert server.cloud_allowed() is False


def test_cloud_opt_in_rejects_unknown_action_without_mutating(monkeypatch):
    monkeypatch.delenv("SONDER_ALLOW_CLOUD", raising=False)
    monkeypatch.setattr(server, "_refresh_live_cloud_tiers", lambda: None)

    assert server.cloud_opt_in("sometimes").startswith("usage:")
    assert "SONDER_ALLOW_CLOUD" not in server.os.environ


def test_cloud_opt_in_is_graded_as_dangerous():
    from sonder_runtime.adapters import command_catalog

    assert "cloud_opt_in" in command_catalog._DANGEROUS
    assert command_catalog._risk_for("cloud_opt_in", server) == "dangerous"
