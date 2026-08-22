"""Regression coverage for the autopilot persistence path boundary."""

from sonder_runtime.adapters.persistence import autopilot_store


def test_database_path_uses_packaged_platform_paths(monkeypatch, tmp_path):
    expected = tmp_path / "autopilot.db"
    calls = []

    def fake_state_path(name, env_var=""):
        calls.append((name, env_var))
        return str(expected)

    monkeypatch.setattr(autopilot_store._platform_paths, "state_path", fake_state_path)

    assert autopilot_store.database_path() == str(expected)
    assert calls == [("autopilot.db", "SONDER_AUTOPILOT_DB")]
