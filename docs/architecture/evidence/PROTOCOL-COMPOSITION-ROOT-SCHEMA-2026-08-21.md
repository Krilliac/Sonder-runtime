# Protocol composition-root schema binding — 2026-08-21

## Scope

The explicit SPEC-5 runtime container now composes a
`ProtocolApplicationFacade` from the same `ToolApplicationFacade` catalog
used by the runtime's tool boundary. The protocol schema retains two distinct
identities: `source_catalog_digest` binds it to the catalog, while the schema
digest covers the complete schema plus the snapshot/event stream contract.
No unauthorized live stream is invented by the inert runtime container;
authorized hosts can add bounded reconnectable stream instances through the
facade's explicit `protocol.stream.create` operation.

## Evidence

```text
python -m pytest -q --basetemp <fresh-user-temp> tests/test_runtime_container_adapter.py tests/test_protocol_application_facade.py tests/test_remaining_client_schema.py
16 passed
```

The composition test proves the runtime exposes the protocol facade, binds its
source catalog digest to the tool catalog, and carries the canonical
snapshot-plus-events stream contract. Facade tests prove authorized stream
creation/closure, reconnect, identity matching, bounded event publication,
and fail-closed authorization.

## Limitations

This remains `implemented_unverified`: the inert runtime container does not
create a live session stream or publish an external SDK package. Session-owned
stream lifecycle and transport authentication remain host/interface concerns.
