"""Apply the shared fleet policy to a host capacity snapshot."""
from ...domain.fleet_briefing import default_breadth


def automatic_fleet_agents(capacity):
    slots = max(1, int(capacity.get("worker_slots") or 1))
    ceiling = max(1, int(capacity.get("agent_ceiling") or
                         capacity.get("max_agents") or max(3, 2 * slots)))
    return default_breadth(ceiling, slots)
