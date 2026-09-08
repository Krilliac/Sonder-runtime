# Deployed two-host artifact mobility acceptance

Status: **not executed by the local rehearsal**. This deployment gate requires
two independently operated hosts and their approved real HTTPS configuration.
Run only with operator/deployment authorization and disposable pre-admitted
content. No loopback test factory, synthetic certificate, disabled verification,
or substituted transport is permitted.

## Private preparation

Pin each deployed revision and obtain an opaque fingerprint of its approved
effective security-relevant configuration. Credential rotation must change the
fingerprint without exposing credentials. Resolve endpoint, certificate,
attestation, source-owner/grant, and key values privately through provisioning.
Never attach those values, config dumps, packet contents, or raw exceptions to
the evidence record.

Keep the source-only spool outside workspace authority and receiver storage.
The sender's receiver may remain disabled. An approved local publisher must
pre-admit disposable binary content larger than one receiver chunk. The receiver
independently provisions identity, source-owner write grant, key, expiry, and
quotas. No public staging API is introduced.

## Required cases

| Case | Action and required observation |
| --- | --- |
| Baseline | Real fixed HTTPS endpoint, correct leaf/attestation pins, and matching source owner: explicit send seals the exact source size/digest/media type. Verify bytes privately on the destination. |
| Certificate mismatch | Deliberately mismatch the leaf pin in isolated approved config. Fail before bearer or upload bytes. Verify controlled receiver-side counters; do not export packet contents. |
| Attestation mismatch | Valid transport but mismatched expected attestation: fail before begin/append, with no appended bytes. |
| Source-owner mismatch | Grant a different source owner: no appended bytes. Matching display labels do not override this check. |
| Key rotation | Interrupt an operation; rotate the peer credential and generation identifier intentionally. Resume of old intent fails its immutable fence before append. Also verify a remotely revoked old key cannot append. |
| Grant/identity rotation | Keep endpoint/label fixed and rotate grant revision or receiver identity remotely. Old resume rejects changed attestation before append; updating the expected pin must not retarget old intent. |
| Certificate rotation | Rotate the served certificate: old pin fails. Updating host provisioning must not authorize the old immutable intent to resume. |
| Quota/capacity | Keep identity and static object-size caps valid while recipient dynamic quota/capacity is insufficient. Begin rejects; appended bytes remain zero. Capability output is not quota proof. |
| Manual resume | Interrupt after a nonzero durable remote offset; close the sender but retain source/journal/receiver state. Verify no autonomous traffic. A live lease/lock blocks replacement; expired-lease recovery is local. Explicit resume reuses the command and durable offset, does not resend accepted bytes, and seals exact content. |
| Surfaces/privacy | Source-only enablement adds no artifact routes. Status/list/capability/close make no peer request. Public output contains only allowed receipt fields, never private endpoint/pin/key/HMAC/payload/path/peer-error values. |

Do not clear leases or edit databases to make a case pass. Receiver verification
may return `awaiting_seal`; confirmation requires another explicit resume.
Attended resolution is required for transient failures. No automatic retry,
failover, ownership transfer, or source deletion is part of acceptance.

## Evidence record

Record exact revisions, opaque host configuration fingerprints, case IDs, UTC
execution time, and constrained pass/fail/not-run observations. Use `source` and
`destination` roles, never hostnames/endpoints. Rotation cases record before/after
fingerprints. Retain private verification details on authorized hosts.

```text
source_revision: <exact commit>
destination_revision: <exact commit>
source_config_fingerprint: <opaque fingerprint>
destination_config_fingerprint: <opaque fingerprint>
executed_at_utc: <timestamp>
case: <case ID>
result: PASS | FAIL | NOT_RUN
observation: <bounded result, such as zero appended bytes or exact sealed content>
```

A local rehearsal cannot fill this record. Any skipped case keeps the deployment
gate incomplete. Full-suite pre-merge verification and independent code review
are separate requirements.
