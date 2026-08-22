import server
from sonder_runtime.platform.location_consent import location_consent


def test_server_production_paths_do_not_call_location_consent_wrapper():
    import ast
    from pathlib import Path

    tree = ast.parse((Path(__file__).parents[1] / "server.py").read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_env_location_consent"
    ]
    assert calls == []


def test_location_consent_is_off_by_default():
    assert location_consent(environ={}) is False


def test_location_consent_accepts_historical_affirmative_values():
    for value in ("1", "true", "TRUE", "yes", "on"):
        assert location_consent(environ={"SONDER_LOCATION_CONSENT": value}) is True


def test_location_consent_rejects_missing_and_non_affirmative_values():
    for value in ("", "0", "false", "no", "off", " enabled "):
        assert location_consent(environ={"SONDER_LOCATION_CONSENT": value}) is False


def test_server_compatibility_helper_delegates_to_platform_policy(monkeypatch):
    monkeypatch.setenv("SONDER_LOCATION_CONSENT", "yes")
    assert server._env_location_consent() is True
    monkeypatch.setenv("SONDER_LOCATION_CONSENT", "off")
    assert server._env_location_consent() is False
