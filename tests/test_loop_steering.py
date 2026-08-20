"""WP2 LOOP-005: immutable steering commands and deterministic order."""

import pytest

from sonder_runtime.application.loop_steering import (
    SteeringCommand,
    SteeringKind,
    SteeringOrder,
    order_commands,
)


def test_commands_are_frozen_and_factory_kinds_are_explicit():
    command = SteeringCommand.follow_up("follow-1", 4, "continue with the report")

    assert command.kind is SteeringKind.FOLLOW_UP
    assert command.order is SteeringOrder.FOLLOW_UP
    with pytest.raises(AttributeError):
        command.text = "changed"  # type: ignore[misc]


def test_order_is_control_first_then_immediate_followup_and_passive_context():
    commands = [
        SteeringCommand.passive_context("context", 1, "known user preference"),
        SteeringCommand.follow_up("follow", 2, "next turn request"),
        SteeringCommand.immediate("immediate", 3, "correct the current answer"),
        SteeringCommand.stop("stop", 4, "finish after this turn"),
        SteeringCommand.cancellation("cancel", 5, "user cancelled"),
    ]

    assert [command.kind for command in order_commands(commands)] == [
        SteeringKind.CANCELLATION,
        SteeringKind.STOP,
        SteeringKind.IMMEDIATE,
        SteeringKind.FOLLOW_UP,
        SteeringKind.PASSIVE_CONTEXT,
    ]


def test_same_kind_preserves_admission_sequence_and_input_is_not_mutated():
    commands = [
        SteeringCommand.follow_up("second", 20, "second"),
        SteeringCommand.follow_up("first", 10, "first"),
    ]
    original = tuple(commands)

    ordered = order_commands(commands)

    assert ordered == (commands[1], commands[0])
    assert tuple(commands) == original


def test_cancellation_and_stop_require_reason_and_content_requires_text():
    with pytest.raises(ValueError):
        SteeringCommand.cancellation("cancel", 0, " ")
    with pytest.raises(ValueError):
        SteeringCommand.stop("stop", 0, "")
    with pytest.raises(ValueError):
        SteeringCommand.immediate("immediate", 0, "")


def test_invalid_sequence_and_command_type_fail_closed():
    with pytest.raises(ValueError):
        SteeringCommand.follow_up("follow", -1, "text")
    with pytest.raises(TypeError):
        order_commands(["not a command"])  # type: ignore[list-item]
