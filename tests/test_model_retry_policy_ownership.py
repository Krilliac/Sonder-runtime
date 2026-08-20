import server
from sonder_runtime.platform import model_retry_policy


def test_server_keeps_compatibility_aliases_for_moved_retry_policy():
    assert server._hosted_overflow_retry_enabled is model_retry_policy.hosted_overflow_retry_enabled
    assert server._overflow_retry_allowed is model_retry_policy.overflow_retry_allowed


def test_loopback_overflow_retry_is_allowed_without_opt_in(monkeypatch):
    monkeypatch.delenv("SONDER_HOSTED_OVERFLOW_RETRY", raising=False)
    assert model_retry_policy.overflow_retry_allowed(
        cloud=False, remote=False, idempotent=False,
    ) is True


def test_hosted_and_remote_overflow_retries_require_idempotency_and_opt_in(monkeypatch):
    monkeypatch.setenv("SONDER_HOSTED_OVERFLOW_RETRY", "true")
    assert model_retry_policy.overflow_retry_allowed(
        cloud=True, remote=False, idempotent=False,
    ) is False
    assert model_retry_policy.overflow_retry_allowed(
        cloud=False, remote=True, idempotent=False,
    ) is False
    assert model_retry_policy.overflow_retry_allowed(
        cloud=True, remote=False, idempotent=True,
    ) is True


def test_hosted_overflow_retry_opt_in_accepts_supported_boolean_spellings(monkeypatch):
    for value in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("SONDER_HOSTED_OVERFLOW_RETRY", value)
        assert model_retry_policy.hosted_overflow_retry_enabled() is True

    for value in ("", "0", "false", "no", "off", "maybe"):
        monkeypatch.setenv("SONDER_HOSTED_OVERFLOW_RETRY", value)
        assert model_retry_policy.hosted_overflow_retry_enabled() is False
