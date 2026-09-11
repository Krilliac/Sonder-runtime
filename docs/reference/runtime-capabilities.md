# Runtime mobility capabilities

Operational capability output is a local configuration/composition projection.
It makes no peer request and does not establish remote health, quota, identity
acceptance, or deployed availability.

| Capability | Meaning |
| --- | --- |
| `fixed_peer_artifact_copy` | Available only when the enabled source-only configuration and fixed outbound binding compose locally. Requires a pre-admitted sealed artifact and explicit operator invocation. |
| `artifact_transfer_transport` | Receiver transport under independent `artifact_transfer` configuration. Source-only settings do not enable it. |
| `automatic_artifact_migration` | Always unavailable. Automatic job/model migration, discovery, rebalance, retry, failover, and ownership transition are not integrated. |

The local CLI alone provides send/resume mutations. Application and REPL views
expose the same constrained read-only operation status/list. HTTP and MCP gain
no outbound controller or source publisher. The public label is a confirmation
string, not an authenticated receiver identity.

See [operations](../operations/artifact-mobility.md),
[configuration](../operations/configuration.md), and the separate
[deployed acceptance gate](../operations/artifact-mobility-two-host-acceptance.md).
