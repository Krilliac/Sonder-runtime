# Scalable Ollama Pool Implementation Plan

> **For agentic workers:** Use subagent-driven development or plan execution
> task-by-task. Each task is independently reviewable.

**Goal:** Replace the hard-coded 16-worker Ollama pool ceiling with explicit
bounded static capacity, then add an opt-in externally admitted membership
path without turning whole-request routing into model sharding.

**Architecture:** Static capacity is the first deliverable. Immutable typed
configuration controls an instance-level roster limit, bounded probes, and
paged status. Dynamic membership is a later control plane: a pure domain
snapshot contract and authenticated adapter feed a controller, which
reconciles an immutable roster into the existing pool without network work
under its admission lock.

**Tech stack:** Python 3.12, standard-library HTTP/TLS transport, dataclasses,
pytest, the existing Flutter app, and existing REPL/HTTP status surfaces.

**Spec:** docs/superpowers/specs/2026-09-06-scalable-ollama-pool-design.md

## Global constraints

- Merge and verify typed Ollama capacity propagation before Task 1.
- Preserve defaults: 16 total workers, one global queue of 32, maximum
  per-worker inflight of 64, and four default probe calls.
- Never increase a limit implicitly from remote input, observed hardware, or a
  worker advertisement.
- Preserve remote consent, HTTPS/trusted-origin validation, no-proxy
  transport, response-size limits, and no-response replay behavior.
- Do not compose inference membership with compute placement, replication,
  artifact transport, quorum, or takeover ownership.
- Keep operational model-sharding and indefinite-scale capability values
  unavailable.
- Use targeted adds and DCO sign-off for every commit.

---

### Task 1: Add a static roster-capacity contract

**Files:**

- Modify: sonder_runtime/platform/config.py
- Modify: tests/test_config.py
- Modify: tests/test_typed_runtime_export.py
- Modify: docs/runbooks/multi-node-ollama.md

**Produces:** Typed values for maximum worker roster, capability-probe
parallelism, capability-probe batch size, and status page size.

- [ ] Write failing configuration tests for default 16, valid 64 and 256,
  below-one rejection, above-256 rejection, a 65-member list under a
  64-worker maximum, and duplicate normalized origins.

- [ ] Run:

      python -m pytest -q tests/test_config.py tests/test_typed_runtime_export.py

  Expect the new static capacity contract to be absent.

- [ ] Add typed TOML parsing, environment parsing, and validation. The maximum
  includes the primary origin. Canonical unique origins are counted. Defaults
  are 16 workers, four probes, 32 workers per probe batch, and 32 status
  records per page. Valid ranges are 1 through 256 workers, 1 through 8
  probes, and 1 through 128 for batch and page size.

- [ ] Document that queue depth remains global, the roster maximum includes
  the primary, and a configured maximum is not a throughput guarantee.

- [ ] Run:

      python -m pytest -q tests/test_config.py tests/test_typed_runtime_export.py
      python scripts/check_doc_links.py
      python scripts/check_architecture.py

- [ ] Commit:

      git add -- sonder_runtime/platform/config.py tests/test_config.py tests/test_typed_runtime_export.py docs/runbooks/multi-node-ollama.md
      git commit -s -m "feat(inference): configure bounded Ollama roster capacity"

### Task 2: Make pool limits instance-owned and status paged

**Files:**

- Modify: sonder_runtime/adapters/inference/ollama_pool.py
- Modify: sonder_runtime/__main__.py
- Modify: sonder_runtime/bootstrap/app.py
- Create: tests/test_ollama_pool_scaling.py
- Modify: tests/test_ollama_pool.py
- Modify: tests/test_ollama_pool_distributed.py

**Produces:** An instance-owned worker maximum, fair bounded probe batches,
and a schema-versioned summary plus cursor-paged worker detail.

- [ ] Write 16, 64, and 256 worker fake-prober tests. Assert that the configured
  maximum rejects the next unique origin, the global queue retains its limit,
  and no more than configured probe parallelism runs at once.

- [ ] Add a fake-clock blocked-probe test proving that one refresh selects no
  more than the configured batch and that successive refreshes make fair
  progress through all stale workers.

- [ ] Write status tests for deterministic ordering, bounded pages, opaque
  cursors, cursor invalidation after a roster-generation change, and absence
  of response bodies, credentials, or arbitrary exception content.

- [ ] Run:

      python -m pytest -q tests/test_ollama_pool_scaling.py tests/test_ollama_pool.py tests/test_ollama_pool_distributed.py

  Expect the source-level maximum and full-list status to fail the new tests.

- [ ] Replace the global worker-count admission with a constructor parameter.
  Keep worker metric labels capped at sixteen plus overflow. Do not derive a
  metric label from origin or member identity. Pass all typed static settings
  at canonical composition roots and retain explicit environment-map
  compatibility behavior.

- [ ] Implement a persistent stale-worker rotation cursor. Select no more than
  one configured batch and submit no more than configured parallel probes.
  Perform network I/O outside the pool condition.

- [ ] Implement summary and page output. Preserve current count keys during a
  compatibility period and add schema version, roster generation, page cursor,
  complete flag, and omitted count.

- [ ] Run:

      python -m pytest -q tests/test_ollama_pool_scaling.py tests/test_ollama_pool.py tests/test_ollama_pool_distributed.py tests/test_typed_runtime_export.py tests/test_operational_capabilities.py
      python scripts/check_architecture.py
      python scripts/check_error_signals.py

- [ ] Commit:

      git add -- sonder_runtime/adapters/inference/ollama_pool.py sonder_runtime/__main__.py sonder_runtime/bootstrap/app.py tests/test_ollama_pool_scaling.py tests/test_ollama_pool.py tests/test_ollama_pool_distributed.py
      git commit -s -m "feat(inference): admit configurable bounded Ollama rosters"

### Task 3: Show honest capacity in app, REPL, and HTTP

**Files:**

- Modify: sonder_runtime/domain/operational_capabilities.py
- Modify: sonder_runtime/interfaces/http/serve.py
- Modify: sonder_runtime/interfaces/repl/facades/status_model.py
- Modify: app/lib/api.dart
- Modify: app/lib/system_screen.dart
- Modify: tests/test_operational_capabilities.py
- Modify: tests/test_serve_auth.py
- Modify: app/test/api_test.dart
- Modify: app/test/widget_test.dart

**Produces:** A read-only summary of configured, eligible, draining, unhealthy,
and omitted workers, plus administrator-authorized bounded detail pages.

- [ ] Write projection tests for a 64-worker summary. Assert request-level
  pooling is available only while a multi-worker pool accepts work, and model
  sharding and indefinite scale remain unavailable.

- [ ] Write API and Flutter parser tests for schema version 2 truncated pages
  and version 1 count-only compatibility.

- [ ] Run:

      python -m pytest -q tests/test_operational_capabilities.py tests/test_serve_auth.py
      flutter test app/test/api_test.dart app/test/widget_test.dart

- [ ] Make summary polling read-only and non-probing. Make detail pagination
  administrator-authorized, request/response bounded, and cursor validated
  without contacting a worker. Render summary counts by default in the REPL
  and app.

- [ ] Run:

      python -m pytest -q tests/test_operational_capabilities.py tests/test_serve_auth.py
      flutter test app/test/api_test.dart app/test/widget_test.dart
      python scripts/check_architecture.py

- [ ] Commit:

      git add -- sonder_runtime/domain/operational_capabilities.py sonder_runtime/interfaces/http/serve.py sonder_runtime/interfaces/repl/facades/status_model.py app/lib/api.dart app/lib/system_screen.dart tests/test_operational_capabilities.py tests/test_serve_auth.py app/test/api_test.dart app/test/widget_test.dart
      git commit -s -m "feat(status): expose bounded inference roster state"

### Task 4: Define pure membership contracts

**Files:**

- Create: sonder_runtime/domain/inference_membership.py
- Create: sonder_runtime/application/ports/inference_membership.py
- Create: tests/test_inference_membership.py
- Modify: docs/architecture/generated/architecture-map.json
- Modify: docs/architecture/generated/architecture-map.md

**Produces:** Immutable worker advertisements, membership snapshots, a source
port, and pure reconciliation results.

- [ ] Write tests for duplicate IDs, malformed identities, non-HTTPS origins,
  invalid protocol version, snapshot size above 4096, expiry inversion,
  generation rollback, same-generation conflict, endpoint replacement, and
  member revocation. Use an injected timezone-aware clock.

- [ ] Run:

      python -m pytest -q tests/test_inference_membership.py

- [ ] Implement standard-library-only immutable values and reconciliation.
  Reconciliation returns additions, activations, drain requests, expirations,
  and a roster generation. It performs no I/O and decides no compute
  ownership.

- [ ] Regenerate the architecture map using the repository architecture
  catalog command.

- [ ] Run:

      python -m pytest -q tests/test_inference_membership.py
      python scripts/check_architecture.py
      python scripts/generate_documentation_catalogs.py --check

- [ ] Commit:

      git add -- sonder_runtime/domain/inference_membership.py sonder_runtime/application/ports/inference_membership.py tests/test_inference_membership.py docs/architecture/generated/architecture-map.json docs/architecture/generated/architecture-map.md
      git commit -s -m "feat(inference): define admitted worker membership contracts"

### Task 5: Reconcile static membership through a controller

**Files:**

- Create: sonder_runtime/adapters/inference/static_membership.py
- Create: sonder_runtime/application/inference_membership/controller.py
- Modify: sonder_runtime/adapters/inference/ollama_pool.py
- Modify: sonder_runtime/bootstrap/app.py
- Create: tests/test_inference_membership_controller.py
- Modify: tests/test_ollama_pool_scaling.py

**Produces:** One reconciliation path for static and future dynamic rosters.

- [ ] Write a deterministic snapshot-sequence test. Assert a new worker is
  probationary, a verified probe activates it, removal blocks new admission
  while an existing request drains, and stale membership expires remote
  admission. Assert endpoint replacement never changes an in-flight endpoint.

- [ ] Run:

      python -m pytest -q tests/test_inference_membership_controller.py tests/test_ollama_pool_scaling.py

- [ ] Implement a static source that converts typed configuration to a
  versioned snapshot. The controller owns its runtime thread and explicit
  close/timeout behavior. Source calls and capability probes occur outside the
  pool condition.

- [ ] Add pool roster reconciliation. Removed or replaced workers drain;
  state leaves only after draining. Preserve retry classification so a
  response-bearing request is never duplicated.

- [ ] Run:

      python -m pytest -q tests/test_inference_membership.py tests/test_inference_membership_controller.py tests/test_ollama_pool_scaling.py tests/test_ollama_pool_distributed.py
      python scripts/check_architecture.py

- [ ] Commit:

      git add -- sonder_runtime/adapters/inference/static_membership.py sonder_runtime/application/inference_membership/controller.py sonder_runtime/adapters/inference/ollama_pool.py sonder_runtime/bootstrap/app.py tests/test_inference_membership_controller.py tests/test_ollama_pool_scaling.py
      git commit -s -m "feat(inference): reconcile bounded worker rosters"

### Task 6: Add externally authenticated membership

**Files:**

- Create: sonder_runtime/adapters/inference/external_membership.py
- Modify: sonder_runtime/platform/config.py
- Modify: sonder_runtime/bootstrap/app.py
- Create: tests/test_external_inference_membership.py
- Modify: tests/test_inference_membership_controller.py
- Modify: docs/runbooks/multi-node-ollama.md

**Produces:** An opt-in external membership source that returns only validated
snapshots or privacy-safe failures.

- [ ] Write adapter tests for redirect rejection, HTTP origin rejection,
  missing client certificate, wrong server identity, untrusted issuer,
  oversized response, invalid signature, expired snapshot, generation
  rollback, and attempts to introduce a new trust authority.

- [ ] Run:

      python -m pytest -q tests/test_external_inference_membership.py tests/test_inference_membership_controller.py

- [ ] Require explicit external mode, cluster ID, protocol version, trust
  anchor, source origin, refresh interval, and bounded snapshot maximum.
  Static remains the default.

- [ ] Implement no-proxy, no-redirect, mutually authenticated HTTPS retrieval
  with bounded reads. Bind issuer and member endpoints to configured trust
  before returning a domain value.

- [ ] Run:

      python -m pytest -q tests/test_external_inference_membership.py tests/test_inference_membership.py tests/test_inference_membership_controller.py tests/test_ollama_pool_scaling.py
      python scripts/check_architecture.py
      python scripts/check_doc_links.py

- [ ] Commit:

      git add -- sonder_runtime/adapters/inference/external_membership.py sonder_runtime/platform/config.py sonder_runtime/bootstrap/app.py tests/test_external_inference_membership.py tests/test_inference_membership_controller.py docs/runbooks/multi-node-ollama.md
      git commit -s -m "feat(inference): admit externally verified worker membership"

### Task 7: Verify migration and private-cluster limits

**Files:**

- Create: tests/test_inference_membership_acceptance.py
- Modify: tests/test_operational_capabilities.py
- Modify: docs/runbooks/multi-node-ollama.md
- Create: docs/runbooks/inference-membership-acceptance.md

**Produces:** Synthetic acceptance evidence and a precise live private-cluster
procedure.

- [ ] Write synthetic 16, 64, and 256 member tests for bounded pages, bounded
  probes, global queue backpressure, capacity accounting, revocation,
  expiration, draining, and response-bearing interruption. Assert sharding
  and indefinite-scale capabilities remain unavailable.

- [ ] Run:

      python -m pytest -q tests/test_inference_membership_acceptance.py

- [ ] Write a disposable-host procedure requiring TLS identities, known model
  inventories, config revision capture, manual revocation, source outage,
  queue saturation, cleanup, and evidence record. State that it proves neither
  model sharding, high-availability takeover, nor unbounded scale.

- [ ] Run:

      python -m pytest -q tests/test_ollama_pool.py tests/test_ollama_pool_distributed.py tests/test_ollama_pool_scaling.py tests/test_inference_membership.py tests/test_inference_membership_controller.py tests/test_external_inference_membership.py tests/test_inference_membership_acceptance.py tests/test_operational_capabilities.py
      python scripts/check_architecture.py
      python scripts/check_requirement_evidence.py
      python scripts/check_error_signals.py
      python scripts/check_history_privacy.py --json
      python scripts/check_doc_links.py
      git diff --check

- [ ] Commit:

      git add -- tests/test_inference_membership_acceptance.py tests/test_operational_capabilities.py docs/runbooks/multi-node-ollama.md docs/runbooks/inference-membership-acceptance.md
      git commit -s -m "test(inference): verify bounded scalable worker membership"

## Plan self-review

- Static capacity is independently shippable and preserves defaults.
- Dynamic membership has separate domain, adapter, controller, and integration
  gates; it is not a configuration-only shortcut.
- Every roster and status path is bounded, paged, and testable with fakes.
- Remote membership cannot bypass endpoint consent or create authority through
  discovery.
- The plan does not alter compute scheduling, takeover, memory, artifact
  mobility, model sharding, or the unavailable indefinite-scale capability.
