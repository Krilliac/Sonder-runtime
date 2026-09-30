"""Side-effect knowledge and its authority, independent of transport schemas.

Unknown is deliberately not false. Only host-owned declarations (including
annotations from an explicitly trusted server) can enable an optimization.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable


class TriState(str, Enum):
    TRUE = "true"
    FALSE = "false"
    UNKNOWN = "unknown"

    def __bool__(self) -> bool:
        raise TypeError("compare a TriState explicitly; UNKNOWN is not false")

    @classmethod
    def from_hint(cls, value: object) -> "TriState":
        if value is True:
            return cls.TRUE
        if value is False:
            return cls.FALSE
        return cls.UNKNOWN


@dataclass(frozen=True)
class ToolTraits:
    read_only: TriState = TriState.UNKNOWN
    destructive: TriState = TriState.UNKNOWN
    idempotent: TriState = TriState.UNKNOWN
    concurrency_safe: TriState = TriState.UNKNOWN
    open_world: TriState = TriState.UNKNOWN
    max_result_bytes: int | None = None
    host_declared: bool = True

    def __post_init__(self) -> None:
        for name in ("read_only", "destructive", "idempotent", "concurrency_safe", "open_world"):
            if not isinstance(getattr(self, name), TriState):
                raise TypeError(f"{name} must be a TriState")
        if type(self.host_declared) is not bool:
            raise TypeError("host_declared must be a Boolean")
        if self.max_result_bytes is not None and (
            type(self.max_result_bytes) is not int or self.max_result_bytes <= 0
        ):
            raise ValueError("max_result_bytes must be a positive integer or None")

    @property
    def is_read_only(self) -> bool:
        return self.host_declared and self.read_only is TriState.TRUE

    @property
    def may_be_destructive(self) -> bool:
        # MCP destructiveHint is meaningful only for tools that may mutate.
        return not self.is_read_only and (
            not self.host_declared or self.destructive is not TriState.FALSE
        )

    @property
    def replay_safe(self) -> bool:
        # Do not infer replay safety from the absence of declared effects.
        return self.host_declared and self.idempotent is TriState.TRUE

    @property
    def can_parallelize(self) -> bool:
        return self.host_declared and self.concurrency_safe is TriState.TRUE

    @property
    def is_open_world(self) -> bool:
        return not self.host_declared or self.open_world is not TriState.FALSE

    def as_dict(self) -> dict:
        return {
            name: getattr(self, name).value
            for name in ("read_only", "destructive", "idempotent", "concurrency_safe", "open_world")
        } | {"max_result_bytes": self.max_result_bytes, "host_declared": self.host_declared}


def traits_from_effects(effects: Iterable[object]) -> ToolTraits:
    """Compatibility for legacy ToolEffect sets; an empty set stays unknown."""
    names = {getattr(effect, "name", str(effect)).lower() for effect in effects}
    if names == {"read_files"}:
        return ToolTraits(read_only=TriState.TRUE, destructive=TriState.FALSE,
                          idempotent=TriState.TRUE, open_world=TriState.FALSE)
    mutations = {"write_files", "delete_files", "execute", "git_write", "package_install", "selfmod"}
    return ToolTraits(
        read_only=TriState.FALSE if names & mutations else TriState.UNKNOWN,
        destructive=TriState.TRUE if "delete_files" in names else TriState.UNKNOWN,
        open_world=TriState.TRUE if "network" in names else TriState.UNKNOWN,
    )
