import pytest

from sonder_runtime.domain.common.ids import (
    AgentId,
    ArtifactId,
    CallId,
    JobId,
    OperationId,
    SessionId,
    StepId,
    TurnId,
    is_id,
    new_id,
)


def test_is_id_rejects_non_hex_payloads():
    assert is_id(new_id("run"), "run")
    assert not is_id("run_" + "z" * 32, "run")
    assert not is_id("run_" + "!" * 32, "run")


@pytest.mark.parametrize(
    "id_type, prefix",
    [
        (SessionId, "session"),
        (TurnId, "turn"),
        (StepId, "step"),
        (CallId, "call"),
        (AgentId, "agent"),
        (JobId, "job"),
        (ArtifactId, "artifact"),
        (OperationId, "operation"),
    ],
)
def test_typed_ids_generate_and_round_trip_stable_serialization(id_type, prefix):
    identifier = id_type.new()

    assert identifier.value == str(identifier) == identifier.serialize()
    assert identifier.value.startswith(prefix + "_")
    assert is_id(identifier, prefix)
    assert id_type.from_serialized(identifier.serialize()) == identifier


def test_typed_ids_reject_wrong_prefix_and_malformed_values():
    valid = "session_" + "a" * 32

    assert SessionId(valid).serialize() == valid
    with pytest.raises(ValueError):
        SessionId("turn_" + "a" * 32)
    with pytest.raises(ValueError):
        SessionId("session_" + "A" * 32)
    with pytest.raises(ValueError):
        SessionId("session_" + "a" * 31)


def test_legacy_helpers_keep_plain_string_compatibility():
    raw = new_id("session")
    typed = SessionId.from_serialized(raw)

    assert isinstance(raw, str)
    assert is_id(raw, "session")
    assert is_id(typed, "session")
    assert not is_id(typed, "turn")
