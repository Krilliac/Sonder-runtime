import server

from sonder_runtime.domain.cancellation_policy import cancellation_requested


def test_server_keeps_identity_compatible_cancellation_policy_alias():
    assert server._cancel_requested is cancellation_requested


def test_cancellation_policy_handles_missing_and_callback_values():
    assert cancellation_requested(None) is False
    assert cancellation_requested(lambda: True) is True
    assert cancellation_requested(lambda: 0) is False


def test_cancellation_policy_fails_closed_when_probe_raises():
    def broken_probe():
        raise RuntimeError("durable state unavailable")

    assert cancellation_requested(broken_probe) is True
