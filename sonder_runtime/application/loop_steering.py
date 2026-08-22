"""Immutable steering commands and ordering rules for the WP2 loop.

This module is deliberately a pure application contract.  It does not send
commands, cancel work, persist commands, or depend on the loop and event
implementations.  An adapter can order a batch at a safe loop boundary and
then apply each command according to its kind.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable


class SteeringKind(str, Enum):
    """The control-plane effect requested by a steering command."""

    CANCELLATION = "cancellation"
    STOP = "stop"
    IMMEDIATE = "immediate_steering"
    FOLLOW_UP = "follow_up"
    PASSIVE_CONTEXT = "passive_context_injection"


class SteeringOrder(int, Enum):
    """Lower values are applied first at the same loop boundary."""

    CANCELLATION = 10
    STOP = 20
    IMMEDIATE = 30
    FOLLOW_UP = 40
    PASSIVE_CONTEXT = 50


_ORDER_BY_KIND = {
    SteeringKind.CANCELLATION: SteeringOrder.CANCELLATION,
    SteeringKind.STOP: SteeringOrder.STOP,
    SteeringKind.IMMEDIATE: SteeringOrder.IMMEDIATE,
    SteeringKind.FOLLOW_UP: SteeringOrder.FOLLOW_UP,
    SteeringKind.PASSIVE_CONTEXT: SteeringOrder.PASSIVE_CONTEXT,
}


@dataclass(frozen=True)
class SteeringCommand:
    """An immutable request to alter loop control or model context.

    ``sequence`` is assigned by the caller at admission time.  It is retained
    as the stable tie-breaker so two commands with the same effect preserve
    arrival order without relying on mutable queue state.
    """

    command_id: str
    kind: SteeringKind
    sequence: int
    text: str = ""
    reason: str = ""

    def __post_init__(self) -> None:
        if not str(self.command_id).strip():
            raise ValueError("command_id must not be empty")
        if self.sequence < 0:
            raise ValueError("sequence must be non-negative")
        if self.kind in {
            SteeringKind.IMMEDIATE,
            SteeringKind.FOLLOW_UP,
            SteeringKind.PASSIVE_CONTEXT,
        } and not str(self.text).strip():
            raise ValueError(f"{self.kind.value} requires text")
        if self.kind in {SteeringKind.CANCELLATION, SteeringKind.STOP} and not str(self.reason).strip():
            raise ValueError(f"{self.kind.value} requires reason")

    @property
    def order(self) -> SteeringOrder:
        return _ORDER_BY_KIND[self.kind]

    @classmethod
    def follow_up(cls, command_id: str, sequence: int, text: str) -> "SteeringCommand":
        return cls(command_id, SteeringKind.FOLLOW_UP, sequence, text=text)

    @classmethod
    def immediate(cls, command_id: str, sequence: int, text: str) -> "SteeringCommand":
        return cls(command_id, SteeringKind.IMMEDIATE, sequence, text=text)

    @classmethod
    def passive_context(cls, command_id: str, sequence: int, text: str) -> "SteeringCommand":
        return cls(command_id, SteeringKind.PASSIVE_CONTEXT, sequence, text=text)

    @classmethod
    def cancellation(cls, command_id: str, sequence: int, reason: str) -> "SteeringCommand":
        return cls(command_id, SteeringKind.CANCELLATION, sequence, reason=reason)

    @classmethod
    def stop(cls, command_id: str, sequence: int, reason: str) -> "SteeringCommand":
        return cls(command_id, SteeringKind.STOP, sequence, reason=reason)


def order_commands(commands: Iterable[SteeringCommand]) -> tuple[SteeringCommand, ...]:
    """Return commands in the immutable WP2 application order.

    Control commands precede content commands.  Cancellation wins over stop;
    stop wins over immediate steering; immediate steering wins over a queued
    follow-up; and passive context is last because it must not change control
    flow.  Arrival order is preserved within each command kind.
    """

    snapshot = tuple(commands)
    if any(not isinstance(command, SteeringCommand) for command in snapshot):
        raise TypeError("commands must contain SteeringCommand values")
    return tuple(sorted(snapshot, key=lambda command: (command.order.value, command.sequence)))


__all__ = ["SteeringCommand", "SteeringKind", "SteeringOrder", "order_commands"]
