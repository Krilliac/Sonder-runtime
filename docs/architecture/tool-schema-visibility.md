# Tool schema visibility

The typed tool registry owns a stable descriptor snapshot of the executable
inventory.  The snapshot copies descriptor schema metadata, so later registry
registration or caller mutation cannot change that captured view.  A
`ToolSchemaSelection` is a separate immutable value carried on each gateway
request; it narrows the registered inventory for that turn and can be reused
after a bounded context reset without restoring mutable registry state.

The gateway checks the selection during schema admission and the typed
invoker checks it again immediately before execution.  A registered tool that
is absent from the active selection therefore receives a `Forbidden` result
even if a caller bypasses the catalog or attempts to invoke the descriptor by
name.

Catalogs expose a summary-first projection (`sonder-tool-summary-v1`) and an
explicit `schema_selection` marker.  The marker describes the selected names,
the selection identity, and whether schemas are available on demand.  Existing
full catalog projections remain available for compatibility while clients
adopt the summary/on-demand flow.
