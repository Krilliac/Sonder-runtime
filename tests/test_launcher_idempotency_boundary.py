import pytest

import sonder_launcher
from sonder_runtime.adapters import launcher_idempotency


def test_root_idempotency_helpers_are_identity_aliases():
    assert sonder_launcher._normalize_idempotency_key is launcher_idempotency.normalize_idempotency_key
    assert sonder_launcher._valid_command_replay is launcher_idempotency.valid_command_replay
    assert sonder_launcher._IDEMPOTENCY_KEY is launcher_idempotency._IDEMPOTENCY_KEY
    assert sonder_launcher._REPLAY_HEADER_NAME is launcher_idempotency._REPLAY_HEADER_NAME
    assert sonder_launcher._REPLAY_HEADER_VALUE is launcher_idempotency._REPLAY_HEADER_VALUE


@pytest.mark.parametrize("value, expected", [(None, ""), ("", ""), ("  valid-key-123  ", "valid-key-123")])
def test_normalize_idempotency_key_preserves_optional_key_contract(value, expected):
    assert launcher_idempotency.normalize_idempotency_key(value) == expected


@pytest.mark.parametrize("value", ["short", "bad key 123", "x" * 129])
def test_normalize_idempotency_key_rejects_invalid_keys(value):
    with pytest.raises(ValueError, match="Idempotency-Key"):
        launcher_idempotency.normalize_idempotency_key(value)


def test_valid_command_replay_accepts_bounded_response():
    assert launcher_idempotency.valid_command_replay(
        {"status": 200, "payload": {"ok": True}, "headers": {"X-Trace": "abc"}}
    )


@pytest.mark.parametrize(
    "value",
    [
        None,
        {"status": True, "payload": {}},
        {"status": 200, "payload": []},
        {"status": 200, "payload": {}, "headers": {"bad name": "value"}},
        {"status": 200, "payload": {}, "headers": {"X": "\n"}},
    ],
)
def test_valid_command_replay_rejects_malformed_or_unsafe_response(value):
    assert launcher_idempotency.valid_command_replay(value) is False
