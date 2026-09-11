"""Bounded source port for verified inference membership, without host I/O."""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Protocol

from ...domain.inference_membership import MAX_ADVERTISEMENTS, MAX_SNAPSHOT_BYTES, MembershipHighWater, MembershipSnapshot


@dataclass(frozen=True, slots=True)
class MembershipSourceLimits:
    max_advertisements: int = MAX_ADVERTISEMENTS
    max_bytes: int = MAX_SNAPSHOT_BYTES
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        for value, ceiling in ((self.max_advertisements, MAX_ADVERTISEMENTS), (self.max_bytes, MAX_SNAPSHOT_BYTES)):
            if type(value) is not int or not 1 <= value <= ceiling:
                raise ValueError("membership source limit is outside its fixed bound")
        if (type(self.timeout_seconds) not in (int, float) or not math.isfinite(self.timeout_seconds)
                or not 0 < self.timeout_seconds <= 30):
            raise ValueError("membership source timeout must be finite and at most 30 seconds")


class MembershipSource(Protocol):
    def read_snapshot(self, *, limits: MembershipSourceLimits) -> MembershipSnapshot:
        """Return one verified snapshot or fail within every supplied bound.

        The source owns configured cluster/issuer and endpoint authority. An
        adapter must bound bytes before parsing/signature verification, pass the
        item/byte limits to MembershipSnapshot.from_signed_envelope, and enforce
        the timeout across its complete read. No partial/truncated snapshot may
        become authority. This port provides no discovery or background loop.
        """
        ...


class MembershipReplayStore(Protocol):
    def read(self) -> MembershipHighWater | None:
        """Read the exact durable authority, failing closed on corrupt state."""
        ...

    def compare_and_advance(self, snapshot: MembershipSnapshot) -> MembershipHighWater:
        """Synchronize durable authority before publishing this snapshot."""
        ...
