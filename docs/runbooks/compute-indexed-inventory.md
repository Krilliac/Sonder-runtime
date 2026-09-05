# Indexed compute inventory and refresh

Production configuration still allows at most 15 remote hosts plus the local host. The 64- and 256-node tests inject synthetic registries; they do not enable larger deployments.

Placement uses configured and observed capability/workload postings to find structurally possible candidates. The existing scheduler still evaluates freshness, health, resource evidence and ranking for every candidate. Model absence can prune selection but cannot prune refresh of a configured-possible host. Local-only, remote-first with optional fallback, and rank-all retain their policy phases. Placement diagnostics include `inventory_scope`; rejected candidate details cover the stated phase and structurally possible subset, not the full fleet. Cached snapshot digests are replaced on observation. Hash work moves to observation time.

Call `ComputeNodeRegistry.capability_candidates(...)` when a caller needs an
indexed capability view before selecting a workload. It intersects all required
capability postings and unions each any-of group, while leaving health,
freshness, resource, consent, and placement-policy checks to the scheduler.
The returned scope uses the same bounded diagnostic metadata as ordinary
indexed candidates and never reserves worker capacity.

The composed refresh coordinator shares eight submitted/running probe slots across this Python process and joins concurrent requests for the same node within one coordinator. Due candidates are traversed in pages of 32, with at most eight pending futures per caller; placement waits for the complete due candidate traversal. Warm fresh observations avoid probes. Failed probes become unhealthy. Candidate-ID collection and homogeneous ranking remain proportional to the matching inventory. This is not an HTTP connection limit or a cross-process fleet-wide authority. The legacy refresh helper remains available for compatibility; production composition uses the coordinator.

## Administrator inventory API

`GET /v1/compute/nodes?limit=32` reads configured inventory without probing. Follow `next_cursor` until `has_more` is false. Limits are 1 through 64, cursors at most 1,024 characters. Membership and ordering are stable within one registry generation; rebuilding configuration invalidates old cursors. Observations are live per page, identified by observation revisions and capture time. Pages can include unobserved hosts. Returned fields omit origin, credentials, workspace paths and model lists.

`POST /v1/compute/nodes/refresh` accepts only a JSON object with optional `limit` and `cursor`. It force-refreshes the remote members of that page and returns the same inventory cursor fields plus `probed_count`, `selected_remote_count`, `refresh_scope: page_only`, and `partial_inventory`. Follow the cursor to refresh further pages. A concurrent request may already have supplied the evidence. A partial page never means a complete fleet refresh. The local host is not probed by this operation.

Both routes require existing administrator authorization. Refresh additionally requires host configuration `compute.allow_remote`; the default remains disabled. Requests cannot supply origins, credentials or new authority. Existing HTTPS and origin rules remain in force. Refresh JSON is capped at 32 KiB (also subject to the host request limit), and serialized page output at 256 KiB; an oversized page returns 413 with a smaller-limit instruction. Eight nonblocking process-wide inventory request slots cover accepted body reads, projection, refresh and response delivery; saturation returns 429. Rejected bodies are closed without draining. This does not bound all HTTP server connections. Unknown fields and invalid cursors return 400; unavailable inventory returns 503.

## Ownership and remaining limits

Inventory and placement snapshots do not reserve or release worker resources. Durable worker admission and process cleanup proof retain that responsibility. No shared physical-host RAM, cross-WSL authority, automated enrollment, HA takeover, inference-routing scalability or production network throughput is established by this change. The legacy diagnostic control-plane snapshot still builds a full inventory, and existing local job-count ceilings are unchanged.

`Application.close_compute(timeout=...)` stops admission and waits for probe completion. A timeout leaves the coordinator closed and occupied probes running until they actually finish; cancellation is not socket cleanup proof. A successful close permits explicit lazy recreation on later use. `close_providers` includes compute cleanup within its timeout. HTTP shutdown closes its default coordinator. Native MCP leaves externally supplied applications reusable by default; the CLI opts into compute cleanup for the application it owns.
