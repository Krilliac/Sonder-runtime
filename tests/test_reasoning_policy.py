from __future__ import annotations

import server
from sonder_runtime.platform import reasoning_policy


def test_exposure_policy_is_disabled_by_default():
    assert reasoning_policy.exposure_enabled(environ={}) is False


def test_exposure_policy_accepts_explicit_truthy_values():
    for value in ("1", "true", "TRUE", "yes", "on"):
        assert reasoning_policy.exposure_enabled(
            environ={"SONDER_EXPOSE_REASONING": value}
        ) is True


def test_exposure_policy_rejects_unrecognized_values():
    for value in ("", "0", "false", "no", "enabled", " true "):
        expected = value.strip().lower() in {"1", "true", "yes", "on"}
        assert reasoning_policy.exposure_enabled(
            environ={"SONDER_EXPOSE_REASONING": value}
        ) is expected


def test_server_keeps_reasoning_exposure_compatibility_behavior(monkeypatch):
    monkeypatch.setenv("SONDER_EXPOSE_REASONING", "yes")
    assert server.reasoning_exposure_enabled() is True
    monkeypatch.setenv("SONDER_EXPOSE_REASONING", "0")
    assert server.reasoning_exposure_enabled() is False
