"""Validated identifier value objects shared across domains.

The serialized form intentionally remains the existing opaque
``<prefix>_<lowercase UUID hex>`` string so callers can cross the current
compatibility boundary without a migration.
"""
from __future__ import annotations

from dataclasses import dataclass
import uuid
from typing import ClassVar, TypeVar


_TypedIdT = TypeVar("_TypedIdT", bound="_TypedId")


def new_id(prefix: str) -> str:
    """Prefixed opaque identifier, e.g. ``new_id("run") -> "run_ab12..."``."""
    if not isinstance(prefix, str) or not prefix or not prefix.isidentifier():
        raise ValueError(f"invalid id prefix {prefix!r}")
    return f"{prefix}_{uuid.uuid4().hex}"


def is_id(value: object, prefix: str) -> bool:
    """Return whether a value has the legacy validated ID representation."""
    if isinstance(value, _TypedId):
        value = value.serialize()
    if not isinstance(value, str) or not isinstance(prefix, str):
        return False
    if not prefix or not prefix.isidentifier() or not value.startswith(prefix + "_"):
        return False
    suffix = value[len(prefix) + 1:]
    # Keep the existing representation while rejecting arbitrary punctuation.
    return len(suffix) == 32 and all(char in "0123456789abcdef" for char in suffix)


@dataclass(frozen=True, slots=True)
class _TypedId:
    """Base for immutable, validated IDs with a stable string boundary."""

    value: str
    PREFIX: ClassVar[str]

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not is_id(self.value, self.PREFIX):
            raise ValueError(f"invalid {self.__class__.__name__}: {self.value!r}")

    @classmethod
    def new(cls: type[_TypedIdT]) -> _TypedIdT:
        return cls(new_id(cls.PREFIX))

    @classmethod
    def from_serialized(cls: type[_TypedIdT], value: str) -> _TypedIdT:
        return cls(value)

    @classmethod
    def from_string(cls: type[_TypedIdT], value: str) -> _TypedIdT:
        """Compatibility spelling for callers that treat IDs as strings."""
        return cls.from_serialized(value)

    def serialize(self) -> str:
        """Return the stable wire representation used by existing callers."""
        return self.value

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class SessionId(_TypedId):
    PREFIX: ClassVar[str] = "session"


@dataclass(frozen=True, slots=True)
class TurnId(_TypedId):
    PREFIX: ClassVar[str] = "turn"


@dataclass(frozen=True, slots=True)
class StepId(_TypedId):
    PREFIX: ClassVar[str] = "step"


@dataclass(frozen=True, slots=True)
class CallId(_TypedId):
    PREFIX: ClassVar[str] = "call"


@dataclass(frozen=True, slots=True)
class AgentId(_TypedId):
    PREFIX: ClassVar[str] = "agent"


@dataclass(frozen=True, slots=True)
class JobId(_TypedId):
    PREFIX: ClassVar[str] = "job"


@dataclass(frozen=True, slots=True)
class ArtifactId(_TypedId):
    PREFIX: ClassVar[str] = "artifact"


@dataclass(frozen=True, slots=True)
class OperationId(_TypedId):
    PREFIX: ClassVar[str] = "operation"
