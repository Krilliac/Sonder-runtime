# Deployment topology and capability status

Sonder defaults to local SQLite control state. A second configured PC can run
private compute jobs. Pooling does not merge session, task, or memory databases
and does not elect a replacement controller. A witness is not part of the local
single-PC profile; a pooled pair requires an independent witness and external
provider before takeover can be considered.

| Profile | Configuration | Behavior |
| --- | --- | --- |
| `single-host` | Default | Local control state; existing optional remote compute configurations continue working. |
| `pooled-pair` | Exactly one distinct `compute.nodes` peer | Explicit two-PC pool; each instance retains its own control state and workers execute legitimate dispatched jobs. |

The typed domain contract also names these modes `single-pc` and `two-pc`.
`single-host` and `pooled-pair` remain accepted configuration aliases. The
contract maps the aliases to the explicit names so status, tests, and future
adapters cannot silently treat a compute pool as a replicated control plane.

`single-host` names the control-state deployment, not a prohibition on remote
workers. Existing configurations with remote workers need no profile change.
Profile membership is configured membership; it does not imply that a peer is
currently reachable. Existing placement freshness and worker admission checks
still determine whether a job can execute.

## Replicated control-state prerequisite

`sonder_runtime.domain.cluster_availability` defines the provider boundary for
the later availability slice. A `ControlStateEvent` identifies the exact
cluster, resource, owner epoch, sequence, and payload digest. An external
provider must return a `ReplicationAcknowledgement` for that exact event with
durable acknowledgement on at least two **data** replicas. Witness IDs are
recorded separately and never count toward that rule. An
`OwnerFencingProvider` must return a matching external `FenceReceipt` before a
takeover can advance the owner epoch.

The pure contract only validates provider-shaped evidence; it does not open
SQLite/PostgreSQL connections, contact a provider, run consensus, or mutate
ownership. `two-pc` therefore continues to report takeover and failback as
unavailable even when a provider capability descriptor has the required shape.
The descriptor is a prerequisite for adapter conformance, not proof of live
replication or high availability. A fence receipt marked `ambiguous`,
`minority`, or `unavailable` is rejected, and a two-node vote alone cannot
override that decision.

For an explicit pair, use the existing private compute authentication, network,
and catalog configuration from [the compute runbook](compute-fabric.md), plus:

```toml
[deployment]
profile = "pooled-pair"
preferred_primary = "workstation"
automatic_takeover = false
automatic_failback = false
```

The preferred primary must name the local compute node or its configured peer.
It is an operator preference displayed in status. It grants no exclusive control
authority, reroutes no requests, and does not block a secondary worker.

Authenticated `/health` includes `deployment`, containing the configured members,
the legacy `profile` value, the canonical `profile_id`, preference,
local-instance control-state scope, and capability reasons. These are integrated
capability/configuration facts, not live peer-health measurements.
Takeover, failback, explicit promotion, acknowledged state replication,
cluster-wide worker-epoch fencing, and quorum are reported unavailable.

Startup rejects `automatic_takeover=true`, `automatic_failback=true`, and an HA
or quorum profile. The same validation runs for TOML, direct typed application
construction, and lifecycle configuration. There is no force-promotion override.
A timeout, disconnected link, preferred-primary label, or explicit operator
request does not prove the former owner's processes are fenced or the surviving
node has all acknowledged data. Existing per-job claim tokens and effect fences
are not cluster ownership epochs.

If one PC disappears, keep using each available instance's own local state and
let existing compute placement report unreachable workers. Do not relabel a
survivor as an authoritative copy of the missing instance's data. Reconnection
restores connectivity, not merged or replicated control state. Automatic
1-to-2-to-1 operation and resuming one durable conversation after controller loss
remain unimplemented until the authority, acknowledged-data, process-fencing,
and client-reconnection infrastructure passes conformance tests.

Rollback removes the deployment configuration section and reverts this slice;
there is no database migration or live node action in this prerequisite change.
