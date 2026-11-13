"""Retire synthetic interactive parents after cooperative cancellation.

Kept outside fleet_store: that durable-ledger module is intentionally not
hot-reloaded, while agent_lanes can refresh in a long-running MCP process.
"""
from __future__ import annotations

import sqlite3
import time

from ...domain.automation import state_machine as _sm


def finish_cancelled_interactive_root(conn: sqlite3.Connection, root_id: str,
                                      now: float | None = None) -> bool:
    """Retire a cancelled synthetic parent once every descendant is terminal."""
    row = conn.execute(
        "SELECT owner_id, owner_pid, role, parent_id, status, cancel_requested "
        "FROM fleet_agents WHERE id=?", (root_id,),
    ).fetchone()
    if (row is None or row["owner_id"] != "interactive-lanes"
            or row["owner_pid"] != 0 or row["role"] != "agent_lane"
            or row["parent_id"] or row["status"] != "running"
            or not row["cancel_requested"]):
        return False
    active = conn.execute(
        """WITH RECURSIVE descendants(id) AS (
            SELECT id FROM fleet_agents WHERE parent_id=?
            UNION ALL
            SELECT child.id FROM fleet_agents AS child
            JOIN descendants AS parent ON child.parent_id=parent.id
        )
        SELECT 1 FROM fleet_agents AS agent
        JOIN descendants ON agent.id=descendants.id
        WHERE agent.status NOT IN
            ('done', 'failed', 'cancelled', 'completed', 'task_drift', 'retried')
            OR agent.in_model_call=1
        LIMIT 1""",
        (root_id,),
    ).fetchone()
    if active is not None:
        return False
    if not _sm.fleet_can_transition("running", "cancelled"):
        raise ValueError("fleet state machine does not allow running -> cancelled")
    finished_at = time.time() if now is None else now
    conn.execute(
        """UPDATE fleet_agents
        SET status='cancelled', activity='cancelled; descendant lanes finished',
            finished_ts=?, updated_ts=?
        WHERE id=? AND status='running' AND cancel_requested=1""",
        (finished_at, finished_at, root_id),
    )
    return True
