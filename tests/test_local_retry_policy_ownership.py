import server
from sonder_runtime.platform import local_retry_policy


def test_server_keeps_compatibility_wrappers_for_local_retry_policy():
    assert server._local_model_retries() == local_retry_policy.local_model_retries()
    assert server._local_retry_delay(2) == local_retry_policy.retry_delay(2)


def test_local_retry_count_is_defaulted_and_bounded(monkeypatch):
    monkeypatch.delenv("SONDER_LOCAL_RETRIES", raising=False)
    assert local_retry_policy.local_model_retries() == 1

    monkeypatch.setenv("SONDER_LOCAL_RETRIES", "999")
    assert local_retry_policy.local_model_retries() == local_retry_policy.MAX_LOCAL_MODEL_RETRIES

    monkeypatch.setenv("SONDER_LOCAL_RETRIES", "not-an-integer")
    assert local_retry_policy.local_model_retries() == 1


def test_local_retry_count_rejects_negative_values(monkeypatch):
    monkeypatch.setenv("SONDER_LOCAL_RETRIES", "-4")
    assert local_retry_policy.local_model_retries() == 0


def test_retry_delay_uses_bounded_exponential_backoff(monkeypatch):
    monkeypatch.setenv("SONDER_LOCAL_RETRY_DELAY_MS", "100")
    assert local_retry_policy.retry_delay(1) == 0.1
    assert local_retry_policy.retry_delay(2) == 0.2
    assert local_retry_policy.retry_delay(5) == 1.0


def test_retry_delay_sanitizes_invalid_and_extreme_configuration(monkeypatch):
    monkeypatch.setenv("SONDER_LOCAL_RETRY_DELAY_MS", "invalid")
    assert local_retry_policy.retry_delay(1) == 0.15

    monkeypatch.setenv("SONDER_LOCAL_RETRY_DELAY_MS", "-50")
    assert local_retry_policy.retry_delay(1) == 0.0

    monkeypatch.setenv("SONDER_LOCAL_RETRY_DELAY_MS", "5000")
    assert local_retry_policy.retry_delay(1) == 1.0
