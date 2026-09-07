# Artifact Mobility Rehearsal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a test-only, Windows-safe two-process loopback rehearsal that proves the existing artifact transfer path can copy a sealed opaque payload between isolated stores.

**Architecture:** A spawned source process starts a private loopback receiver, uploads a deterministic payload larger than 2 MiB through the existing client, and publishes only its opaque artifact receipt and loopback origin to the parent. A separately spawned destination process starts a different receiver and relays bounded source `read_range` responses into the existing destination upload client. Both child processes own distinct roots, bindings, listener ports, and shutdown paths; the parent asserts their reports and joins them under finite deadlines.

**Tech Stack:** Python stdlib multiprocessing (`spawn`), `ThreadingHTTPServer`, existing `ArtifactTransferBinding`, `HttpsArtifactTransferPeer`, and `ArtifactTransferClient`.

**Spec:** This test is acceptance evidence for the artifact transfer path only. It must not add production composition, configuration, listeners, or claims of independent-node failover, TLS, restart recovery, automatic migration, or remote deployment.

## Global Constraints

- Use `multiprocessing.get_context("spawn")`; no fork-only behavior.
- Use numeric `127.0.0.1` origins only through `HttpsArtifactTransferPeer.for_test_loopback`.
- Keep every source read and receiver chunk at or below the existing 1 MiB protocol bound.
- Pass credentials only inside child-local configuration/callbacks; do not put them in reports, assertion messages, or logs.
- Ensure normal completion closes bindings/listeners and joins both child processes before the test returns.

---

### Task 1: Add the test-only rehearsal

**Files:**
- Create: `tests/test_artifact_mobility_rehearsal.py`
- Create: this plan

**Interfaces:**
- Consumes: `ArtifactTransferBinding`, `HttpsArtifactTransferPeer.for_test_loopback`, `ArtifactTransferClient.upload`, and `HttpsArtifactTransferPeer.read_range`.
- Produces: one focused pytest acceptance test with process reports containing only PIDs, opaque receipts, byte sizes, digests, and loopback endpoints.

- [x] **Step 1: Write the acceptance test and test-local helpers**

Create top-level spawn targets for source and destination workers. Give each worker its own `SonderConfig`/`ArtifactTransferConfig`, private root, binding, and `ThreadingHTTPServer`. Add a `PeerRangeStream` test helper whose `read(size)` asks the source peer for one bounded range and whose `seek(offset)` rejects invalid positions.

- [x] **Step 2: Run the new test and inspect the first result**

Run:

```powershell
& $runner -m pytest -q tests/test_artifact_mobility_rehearsal.py
```

The test must either demonstrate two child processes, distinct roots/endpoints, a sealed destination receipt, and verified bytes, or surface an existing transport boundary that makes this test unsafe or disproportionate.

- [x] **Step 3: Keep the harness bounded and accurate**

Use a payload of `2 * 1024 * 1024 + 37` bytes, fixed queue/join/poll deadlines, and a `finally` block that signals the source shutdown and terminates a stuck child only as failed-test cleanup. Assert the destination digest and size against the source receipt and read destination ranges through the destination peer before reporting success.

- [x] **Step 4: Verify focused artifact-transfer regression coverage**

Run:

```powershell
& $runner -m pytest -q tests/test_artifact_mobility_rehearsal.py tests/test_artifact_transfer_production_http.py tests/test_artifact_transfer_composed_streaming.py
```

- [x] **Step 5: Commit the isolated test evidence**

```powershell
git add docs/superpowers/plans/2026-09-06-artifact-mobility-rehearsal.md tests/test_artifact_mobility_rehearsal.py
git commit -s -m "test(artifact): rehearse loopback mobility path"
```

## Limits recorded by this plan

The resulting test demonstrates an isolated two-process loopback transfer path using HTTP that is deliberately allowed only by the test-only peer factory. It does not prove two independent machines, production TLS/auth deployment, power-loss survival, surviving source loss after transfer, automatic ownership/takeover, replicated memory, cross-node task migration, or general artifact replication.
