"""Apply the shared fleet policy to a host capacity snapshot."""
from __future__ import annotations

from typing import TYPE_CHECKING, cast

from ...domain.fleet_briefing import default_breadth

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import SupportsIndex, SupportsInt


def automatic_fleet_agents(capacity: Mapping[str, object]) -> int:
    slots = max(
        1,
        int(cast("int | str | SupportsInt | SupportsIndex", capacity.get("worker_slots") or 1)),
    )
    ceiling = max(
        1,
        int(
            cast(
                "int | str | SupportsInt | SupportsIndex",
                capacity.get("agent_ceiling")
                or capacity.get("max_agents")
                or max(3, 2 * slots),
            )
        ),
    )
    return default_breadth(ceiling, slots)
