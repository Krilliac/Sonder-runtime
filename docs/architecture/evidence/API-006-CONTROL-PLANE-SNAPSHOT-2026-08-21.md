# API-006 operator control-plane snapshot evidence

`ControlPlaneSnapshot` is the typed, read-only operator view over the
control-plane sections required by the master specification: sessions, plans,
approvals, jobs, agents, model/hardware, context, memory explanations,
extensions, training, selfmod, updates, health, and startup authorities.

The contract freezes nested records, validates section identity and revision,
serializes to a stable JSON shape, and provides a digest for polling,
comparison, and audit records. It is a presentation projection and does not
become a second source of truth or expose mutation operations.

Focused evidence:

```text
python -m pytest tests/test_control_plane_snapshot.py -q
```

This proves the application-level snapshot contract. A deployed HTTP/MCP
operator route that assembles every section from live providers remains a
separate integration obligation.
