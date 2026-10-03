"""Own process-local fleet slots through the legacy module's shared state."""
import sqlite3


class ReservationLedger:
    """Keep legacy scalar writes and reloads on the same dictionary and lock."""

    def __init__(self, state):
        self._state = state
        # A first legacy update can identify only cached local live agents.
        # Keep unknown scalar surplus until restart rather than invent slots.
        if "_RESERVED_AGENT_IDS" not in state:
            with state["_LOCK"]:
                state["_RESERVED_AGENT_IDS"] = {
                    agent_id for agent_id, row in state["_AGENTS"].items()
                    if row.get("owner_id") == state["_OWNER_ID"]
                    and row.get("status") in ("queued", "running")
                }
                state["_RESERVED_SLOTS"] = max(
                    state["_RESERVED_SLOTS"], len(state["_RESERVED_AGENT_IDS"]),
                )

    def reserve(self, agent_id):
        state = self._state
        with state["_LOCK"]:
            owned = state["_RESERVED_AGENT_IDS"]
            if agent_id in owned:
                raise RuntimeError("fleet agent ID collision; retry orchestration")
            owned.add(agent_id)
            state["_RESERVED_SLOTS"] = max(state["_RESERVED_SLOTS"] + 1, len(owned))

    def release(self, agent_id):
        state = self._state
        with state["_LOCK"]:
            owned = state["_RESERVED_AGENT_IDS"]
            if agent_id not in owned:
                return False
            owned.remove(agent_id)
            state["_RESERVED_SLOTS"] = max(state["_RESERVED_SLOTS"] - 1, len(owned))
            return True

    def release_cancelled(self, row):
        if (row and row.get("owner_id") == self._state["_OWNER_ID"]
                and row.get("status") == "cancelled" and not row.get("in_model_call")):
            self.release(str(row.get("id") or ""))

    def create(self, agent_id, create_agent, row, owner_id, pid, principal_id, principal_secret):
        self.reserve(agent_id)
        try:
            if principal_id:
                return create_agent(row, owner_id, pid, principal_id=principal_id,
                                    principal_secret=principal_secret)
            return create_agent(row, owner_id, pid)
        except BaseException as exc:
            self.release(agent_id)
            if isinstance(exc, sqlite3.IntegrityError):
                raise RuntimeError("fleet agent ID collision; retry orchestration") from exc
            raise
