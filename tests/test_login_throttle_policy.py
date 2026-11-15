"""Unit tests for the per-username login backoff and its env configuration."""
from __future__ import annotations

import pytest

from sonder_runtime.adapters.security import login_throttle as adapter
from sonder_runtime.domain.login_throttle import LoginThrottlePolicy, UsernameBackoff

POLICY = LoginThrottlePolicy(threshold=3, decay_seconds=10.0, max_entries=4)


def test_backoff_starts_at_threshold_and_decays():
    backoff = UsernameBackoff(POLICY)
    for _ in range(2):
        backoff.record_failure("a", 0.0)
    assert backoff.check("a", 0.0).allowed
    backoff.record_failure("a", 0.0)
    decision = backoff.check("a", 0.0)
    assert not decision.allowed and decision.retry_after_seconds == 10
    assert backoff.check("a", 10.0).allowed


def test_refused_attempts_do_not_extend_the_window():
    backoff = UsernameBackoff(POLICY)
    for _ in range(3):
        backoff.record_failure("a", 0.0)
    for _ in range(1000):
        assert not backoff.check("a", 1.0).allowed  # check never records
    assert backoff.check("a", 10.0).allowed


def test_window_is_bounded_and_success_clears():
    backoff = UsernameBackoff(POLICY)
    for _ in range(3):
        backoff.record_failure("a", 0.0)
    assert backoff.check("a", 10_000.0).allowed
    backoff.record_failure("a", 10_000.0)
    backoff.record_success("a")
    assert len(backoff) == 0


def test_memory_is_bounded_and_decayed_entries_go_first():
    backoff = UsernameBackoff(POLICY)
    backoff.record_failure("old", 0.0)
    for index in range(10):
        backoff.record_failure(f"u{index}", 1000.0 + index)
    assert len(backoff) <= POLICY.max_entries
    assert "old" not in backoff._entries


def test_policy_rejects_non_positive_values():
    with pytest.raises(ValueError):
        LoginThrottlePolicy(threshold=0)


def test_env_overrides_policy(monkeypatch):
    monkeypatch.setenv(adapter.THRESHOLD_ENV, "5")
    monkeypatch.setenv(adapter.DECAY_SECONDS_ENV, "2.5")
    monkeypatch.setenv(adapter.MAX_ENTRIES_ENV, "not-a-number")
    policy = adapter.policy_from_env()
    assert policy.threshold == 5
    assert policy.decay_seconds == 2.5
    assert policy.max_entries == LoginThrottlePolicy().max_entries


def test_configure_replaces_state():
    adapter.configure(LoginThrottlePolicy(threshold=1))
    adapter.record_failure("x")
    assert not adapter.check("x").allowed
    adapter.configure(None)
    assert adapter.check("x").allowed


def _in_backoff(backoff, key="a"):
    for _ in range(POLICY.threshold):
        backoff.record_failure(key, 0.0)
    assert not backoff.check(key, 0.0).allowed


def test_trusted_client_bypasses_the_window():
    backoff = UsernameBackoff(POLICY)
    _in_backoff(backoff)
    assert backoff.check("a", 0.0, trusted=True).allowed


def test_familiar_client_bypasses_the_window_only_for_its_user():
    backoff = UsernameBackoff(POLICY)
    backoff.record_success("a", 0.0, "198.51.100.7")
    _in_backoff(backoff)
    assert backoff.check("a", 1.0, client="198.51.100.7").allowed
    assert not backoff.check("a", 1.0, client="198.51.100.8").allowed
    assert not backoff.check("a", 1.0).allowed
    _in_backoff(backoff, "b")
    assert not backoff.check("b", 1.0, client="198.51.100.7").allowed


def test_familiarity_expires_and_is_bounded():
    policy = LoginThrottlePolicy(threshold=3, decay_seconds=10.0, max_entries=4,
                                 familiar_ttl_seconds=100.0, max_familiar=2)
    backoff = UsernameBackoff(policy)
    backoff.record_success("a", 0.0, "1.1.1.1")
    assert backoff.is_familiar("a", "1.1.1.1", 99.0)
    assert not backoff.is_familiar("a", "1.1.1.1", 100.0)
    for index in range(5):
        backoff.record_success("a", 1.0, f"2.2.2.{index}")
    assert len(backoff._familiar) <= 2


def test_eviction_keeps_entries_in_active_backoff():
    backoff = UsernameBackoff(POLICY)  # max_entries=4, threshold=3
    for _ in range(3):
        backoff.record_failure("target", 0.0)
    for index in range(20):  # junk-username spray, one failure each
        backoff.record_failure(f"junk{index}", 0.0)
    assert len(backoff) <= POLICY.max_entries
    assert not backoff.check("target", 0.0).allowed
