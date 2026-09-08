# Artifact mobility configuration

This page covers fixed-peer artifact-copy settings. The broader runtime guide
remains [the configuration wiki](../wiki/03-configuration.md).

Mobility defaults to disabled. These are host provisioning settings, not CLI
invocation options. See the [operations guide](artifact-mobility.md) for canonical
first-start files and the pinned Application lifecycle.

| Section / field | Contract |
| --- | --- |
| `artifact_mobility_source.enabled` | Enable a private source-only namespace; does not enable `artifact_transfer`. |
| `store_dir` | Absolute private spool outside configured workspace/home and file-tool roots, separate from receiver storage. |
| `principal_id`, `project_id`, `source_owner_id` | Fixed source scope. The destination independently grants the matching source owner. |
| `max_object_bytes` | Default 256 MiB; 1 byte–64 GiB. |
| `total_bytes` | Default 2 GiB; 1 byte–128 GiB, at least the object cap. |
| `ttl_seconds` | Source retention: default 86400; 1–86400 seconds. |
| `artifact_mobility.enabled` | Enable outbound dispatch; requires enabled source configuration. |
| `destination_label` | Public opaque label, `[A-Za-z0-9][A-Za-z0-9_-]{0,127}`. Never a peer identity or endpoint. |
| `destination_origin` | Fixed canonical HTTPS origin with explicit port; no user info, query, fragment, or non-root path. |
| `destination_tls_certificate_sha256` | Exact DER leaf-certificate SHA-256, 64 lowercase hex characters. |
| `expected_recipient_attestation_sha256` | Expected canonical grant/identity attestation digest, 64 lowercase hex characters. |
| `destination_credential_id` | Host-owned credential generation identifier. |
| `max_object_bytes` | Default 256 MiB; 1 byte–64 GiB, no larger than source cap. Does not establish dynamic receiver quota. |
| `attempt_timeout_seconds` | Default 30; 1–30 seconds. |
| `attempt_lease_seconds` | Default 90; 2–3600 seconds and greater than timeout. |
| `receipt_ttl_seconds` | Default 7 days; 60 seconds–31 days. |
| `max_live_operations` | Default 64; 1–256. |

Provision `SONDER_ARTIFACT_MOBILITY_PEER_KEY` in canonical protected `sonder.env`;
never put it on the command line or in reports. It must be distinct from this
host's admin/auth/receiver keys. The destination independently provisions its
corresponding receiver credential and write grant. Neither compute settings nor
a display label or caller-selected file authorizes copying. The private host
bootstrap ignores ambient environment overrides.

The destination configures its own `artifact_transfer` receiver identity,
source-owner match, grant identity/revision, expiry, write permission, object cap,
and quota. A write-only grant gives no general artifact reads or browsing;
mobility receipt access is transfer-bound.

Source scope, destination, pin, grant, or credential changes do not retarget an
existing operation. Intentional rotation requires host provisioning and a new
operation where old immutable fences no longer match. Validate with the
[two-host acceptance plan](artifact-mobility-two-host-acceptance.md).
