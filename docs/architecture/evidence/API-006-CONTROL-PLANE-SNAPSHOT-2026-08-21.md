# API-006 operator control-plane snapshot evidence

`ControlPlaneSnapshot` is the typed, read-only operator view over the
control-plane sections required by the master specification: sessions, plans,
approvals, jobs, agents, model/hardware, context, memory explanations,
extensions, training, selfmod, updates, health, and startup authorities.

The contract freezes nested records, validates section identity and revision,
serializes to a stable JSON shape, and provides a digest for polling,
comparison, and audit records. It is a presentation projection and does not
become a second source of truth or expose mutation operations.

`ControlPlaneSnapshotService` now assembles the complete projection from
explicitly injected read-only section providers. Providers are required for
every section, invoked in a stable order, limited to 1,024 records per
section, and converted to a failed snapshot if any provider errors or returns
invalid data. This prevents an operator from mistaking a partial view for a
healthy complete view.

Focused evidence:

```text
python -m pytest tests/test_control_plane_snapshot.py -q
python -m pytest tests/test_control_plane_service.py -q
```

These prove the application-level snapshot contract and provider-backed live
assembly. A deployed HTTP/MCP operator route and live provider wiring remain
separate integration obligations.
