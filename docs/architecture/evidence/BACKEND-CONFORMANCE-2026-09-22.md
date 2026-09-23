# Backend conformance routing slice

`BackendConformanceRecord` is a bounded, provider-neutral record for one
backend/model. `RecentCapabilityEvidence` preserves a bounded set of records
across writes and reopen, replacing the JSON file atomically while applying a
configurable age limit. Profiles carry their backend identity into the lookup.

`run_smoke_probes` exercises a deterministic provider seam for plain chat,
structured tool continuation, and cancellation. It does not contact Ollama or
any hosted model. Probe results carry stable reason codes and failed probes are
never treated as passing evidence. Deterministic fake-provider records are
explicitly marked synthetic and cannot authorize routing; only an explicitly
verified non-synthetic provider record can do so.

`CapabilityRouter` accepts the evidence store as an optional admission policy.
When configured, semantic routes always require recent plain-chat evidence and
structured routes additionally require the structured continuation probe. A
missing, stale, or failed record raises `CapabilityRoutingError` with the
corresponding `reason_code` before a route decision is returned. Existing
callers that do not provide evidence retain the prior profile-only behavior;
the composition root still needs to opt the live local route into this store.
