"""Bounded read projections of persisted background work, without reconciliation.

Page master rows before their children so a busy fleet cannot hide its own
parent. These reads do not claim work, start workers, or change run status.
"""
from contextlib import closing

import sonder_runtime.adapters.persistence.autopilot_store as autopilot_store
import sonder_runtime.adapters.persistence.fleet_store as fleet_store


def fleet_snapshot(*, limit=100, project=""):
    cap = max(1, min(int(limit), 200))
    project_sql = " AND project=?" if project else ""
    project_args = (project,) if project else ()
    with closing(fleet_store._connect()) as conn:
        conn.execute("BEGIN")
        masters = conn.execute(
            "SELECT * FROM fleet_agents WHERE role='master' AND COALESCE(parent_id,'')=''"
            + project_sql + " ORDER BY started_ts DESC, id DESC LIMIT ?",
            (*project_args, cap + 1),
        ).fetchall()
        rows = []
        for source in masters[:cap]:
            master = dict(source)
            children = conn.execute(
                "SELECT * FROM fleet_agents WHERE parent_id=? ORDER BY started_ts DESC, id DESC LIMIT 257",
                (master["id"],),
            ).fetchall()
            master["child_counts"] = {
                row[0]: row[1] for row in conn.execute(
                    "SELECT status, COUNT(*) FROM fleet_agents WHERE parent_id=? GROUP BY status",
                    (master["id"],),
                )
            }
            master["children_truncated"] = len(children) > 256
            rows.append(master)
            rows.extend(dict(row) for row in children[:256])
    return {"agents": rows, "has_more": len(masters) > cap}


def autopilot_snapshot(*, limit=100, request_owner=None):
    cap = max(1, min(int(limit), 200))
    scope_sql = " WHERE request_owner=?" if request_owner is not None else ""
    scope_args = (request_owner,) if request_owner is not None else ()
    with closing(autopilot_store._connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM autopilot_runs" + scope_sql
            + " ORDER BY created_ts DESC, id DESC LIMIT ?", (*scope_args, cap + 1),
        ).fetchall()
    return {"runs": [dict(row) for row in rows[:cap]], "has_more": len(rows) > cap}
