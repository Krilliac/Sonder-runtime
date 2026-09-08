# Disposable private inference membership acceptance

This procedure validates bounded whole-request routing on an explicitly
provisioned private test cluster. The automated acceptance matrix uses 16, 64,
and 256 synthetic members; it is not evidence of that many deployed workers or
of their throughput. No live cluster has been validated by adding this runbook.

This does not establish model sharding, high-availability takeover, automatic
failback, replication, compute placement, or unbounded scale. One accepted
request still runs on one worker. The configured roster ceiling is at most 256,
including the primary, local reservations and draining states. The separate
advertisement ceiling of at most 4,096 is not a 4,096-worker scheduler.

## Before starting

Use disposable hosts, an isolated private network and test-only prompts. Have
the owning operator authorize the exact coordinator, registry and workers.
Do not reuse a production identity, state directory, model prompt, credential,
listener or DNS zone. Do not synchronize any existing Node1 workspace or change
an installed runtime for this procedure.

Provision the following and record their revisions in the evidence record:

| Prerequisite | Required evidence |
| --- | --- |
| Application | Exact source commit, dependency lock revision, OS and runtime versions. |
| Identity | Test private CA, registry and worker certificates, a coordinator client identity required by both registry and every worker, and a pinned Ed25519 snapshot signing public key. Record certificate/key fingerprints and expiry only; never copy private keys. |
| Endpoint policy | Each stable member ID maps to one exact canonical HTTPS origin with explicit port, exact DNS or IP SAN, and bounded allowed CIDRs. Capture the registry origin/SAN/CIDRs separately. No wildcard names, suffix rules, CN fallback or default route CIDRs. |
| File custody | Private, owner-controlled local directories and single-link regular authority/credential files. Use absolute OS-native paths; no network paths, links or public permissions. |
| Inventory | Known model names, revisions and digests per worker, plus locally configured inflight limits. Pull models before the run; signed advertisements alone are not capability evidence. |
| Bounds | Primary-inclusive roster maximum, per-worker inflight cap, global queue depth, probe batch/parallelism, page size, capability TTL, membership interval and snapshot byte/item caps. Start with small limits supported by the hosts. |
| Replay protection | Dedicated runtime state root; record the private high-water directory custody, then capture each accepted `{cluster, issuer, generation, digest}` revision and signed snapshot expiry. Never reset replay state to make a test pass. |
| Observability | Private registry/worker access counts, coordinator aggregate status, administrator pages, request correlation IDs, and a monotonic timeline. Disable payload/body and credential logging. |

Use the typed external configuration and secret-backed client certificate/key
settings described in [multi-node Ollama](multi-node-ollama.md). Set
`local_fallback = false` for the first run. If a later run tests `true`, retain
the exact configured loopback origins and document the separate local capacity.
Do not treat a local result as evidence that remote admission succeeded.

The embedding owner must explicitly start the composed membership controller
or call `application.inference_membership.refresh(timeout_seconds=30)`; ordinary
status, CLI entry and inference do not enroll members or start this lifecycle.
The administrator cache refresh is not a membership source refresh.
Default embeddings remain unavailable while an external source is owned;
this procedure tests pool inference, not an embedding bypass.

## Execution and pass criteria

1. **Establish an offline baseline.** Run the focused synthetic suite below at
   the recorded commit. Construct the disposable typed application without
   starting its membership lifecycle. Observe zero registry/worker calls and
   remote workers in probation. Repeated default `status()` calls must retain
   cached aggregate counts, show unknown freshness where appropriate and
   disclose no origins or inventory. Verify access counters do not move.

2. **Admit the known roster.** Publish a correctly signed, unexpired snapshot
   using the configured cluster, issuer, protocol and exact member policies.
   Explicitly refresh the controller. Confirm the durable high-water revision
   was accepted before new admission. Workers become active only after fresh
   local capability evidence. With a roster larger than one configured batch,
   repeat explicit refreshes and record fair progress; no pass probes more
   than the configured batch or runs more than the configured parallelism.

3. **Verify status and authorization.** Default status must remain non-probing.
   Use an administrator to request cached pages of size 1 and 128 with
   `ollama_pool_admin_status(refresh=false, ...)`, following returned cursors.
   Check deterministic coverage, no duplicates, complete records and actual
   UTF-8 byte lengths no greater than 65,536. Model previews are limited to
   eight sanitized 128-character names; full inventories and raw errors must
   be absent. An explicit `refresh=true` triggers only one configured stale
   batch. Unauthorized requests and invalid/stale/cross-principal cursors must
   cause zero refresh calls. Retain redacted error categories, not error bodies.

4. **Route and saturate.** Send bounded, individually correlated requests for
   models known to exist on particular workers. Verify each request reaches
   one eligible worker and capacity decreases/releases correctly. Hold a
   finite set of requests until configured capacity is occupied, admit exactly
   the global queue depth of waiters, then verify the next request receives
   backpressure. The queue must not be multiplied by worker count. Release
   held requests and verify inflight/waiting counters return to zero. Use
   operator-controlled release and a fixed deadline; stop the run if host
   resources exceed its approved budget.

5. **Revoke manually and drain.** Start one bounded request on a chosen worker,
   then publish a strictly newer signed snapshot revoking its member ID.
   Explicitly refresh membership and capture the new high-water value. New
   requests must not reach the revoked worker; the existing request keeps its
   original endpoint until completion or failure. Repeat with removal. If a
   response has already begun and is interrupted, verify exactly one dispatch
   for that logical request, including when it was marked idempotent. A later
   operator-issued request is a different logical request, not replay.

6. **Exercise a source outage and expiry.** Make only the disposable registry
   unavailable. Before signed expiry, the last accepted roster remains usable
   only while its capability evidence is fresh. Record failed refreshes without
   high-water deletion or downgrade. After expiry, verify remote admissions
   stop even before another successful refresh. Existing requests drain;
   default status must not repair the outage. Restore service using a valid
   newer generation and explicit owner refresh. Do not claim this demonstrates
   automatic failback or high-availability ownership transfer.

7. **Run the isolated DNS-rebinding negative test.** Use the disposable resolver
   fixture and test-only names/addresses under operator control. Supply a
   mixed answer containing an address outside the configured CIDRs and verify
   rejection before any connector call. For an accepted answer, verify the
   connector receives the already validated numeric address and there is no
   second DNS resolution to change the destination. A wrong exact SAN/SNI or
   untrusted certificate must fail closed. Record resolver/connector counters
   and redacted rejection categories. Never direct this test at a third-party
   or production endpoint; the offline adapter tests below are sufficient if
   controlled resolver instrumentation is unavailable, and the live item must
   then be recorded as not run.

8. **Check identity churn and restart.** With private test traffic, record the
   emitted metric label set before removal/reordering/re-admission. The first
   sixteen named labels must remain bound to their original admitted identity;
   later identities use `overflow`. No origin, member ID or model appears as a
   metric label. Restart only the disposable coordinator against the same
   protected high-water directory. Older generations and same-generation
   digest conflicts must remain rejected. Never delete or restore an older
   state directory to simulate recovery. A privileged whole-directory rollback
   across restart is outside the local adapter's monotonic-anchor guarantee.

## Offline gates

From the source checkout with its project Python dependencies installed:

```powershell
python -m pytest -q tests/test_inference_membership_acceptance.py
python -m pytest -q tests/production/test_config.py tests/test_ollama_pool.py tests/test_ollama_pool_distributed.py tests/test_ollama_pool_scaling.py tests/test_inference_membership.py tests/test_inference_membership_controller.py tests/test_external_inference_membership.py tests/test_membership_high_water.py tests/test_inference_membership_acceptance.py tests/test_operational_capabilities.py tests/test_server_helpers.py tests/test_serve_auth.py
python scripts/check_architecture.py
python scripts/check_requirement_evidence.py
python scripts/check_error_signals.py
python scripts/check_history_privacy.py --json
python scripts/generate_documentation_catalogs.py --check
python scripts/check_doc_links.py
git diff --check
```

The synthetic suite uses fake clocks, probes, transports and signed fixtures.
Its full-roster saturation uses real scheduler reservation/release operations
without creating 256 OS threads. It measures bounds and accounting, not live
load, TLS interoperability, physical memory use or throughput. The external
adapter suite provides separate local TLS/resolver and replay-protection
evidence; neither suite upgrades a live checklist item to passed.

## Cleanup and evidence record

Stop test submission, release bounded held requests, and close the application
providers/controller with a finite timeout. If close reports incomplete work,
record it and finish owned cleanup before destroying hosts. Revoke the test
client identity and test signing authority, close disposable listeners, remove
only the explicitly provisioned DNS records/firewall entries, and destroy test
hosts through their owner. Preserve the evidence and protected high-water
directory under the approved retention policy before disposing of that exact
test environment. Do not delete a production state directory or credentials.

For every run record: operator, UTC start/end, commit/dependency/config hashes,
host count and OS, identities/policy fingerprints, model inventory digest,
limits, high-water revisions and snapshot expiries, each step's pass/fail/not-run
result, observed counts/peak concurrency/byte lengths, logical request dispatch
counts, test commands and exit codes, cleanup completion and limitations.
Store private topology evidence in restricted custody; publish only redacted
aggregate results. Never attach private keys, tokens, prompt/response bodies,
workspace paths or raw exceptions. An incomplete or failed gate remains visible
in the record and prevents a live readiness claim.
