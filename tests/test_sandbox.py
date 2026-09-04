"""Retained legacy value API; execution refusal is tested separately.

The former execution tests required unsafe container-to-host fallback. Actual
production execution remains covered by isolated-runner and process-job suites.
"""
import pytest

from sonder_runtime.adapters.execution.sandbox import (
    IsolationLevel, SandboxPolicy, SandboxResult,
)


@pytest.mark.parametrize("exit_code,timed_out,expected", [
    (0, False, True), (1, False, False), (0, True, False),
])
def test_legacy_result_value_semantics(exit_code, timed_out, expected):
    assert SandboxResult(exit_code=exit_code, timed_out=timed_out).ok is expected


def test_legacy_policy_values_are_retained(monkeypatch):
    monkeypatch.setenv("PATH", "test-path")
    monkeypatch.setenv("UNRELATED_SECRET", "never-included")
    policy = SandboxPolicy(env_allowlist=("PATH",))
    assert policy.effective_env() == {"PATH": "test-path"}
    assert policy.level is IsolationLevel.SUBPROCESS
    assert policy.timeout_seconds == 30.0
    assert policy.max_memory_mb == 512
    assert not policy.allow_network
