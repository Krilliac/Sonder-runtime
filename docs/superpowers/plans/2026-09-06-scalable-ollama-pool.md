# Scalable Ollama Pool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` or
> `superpowers:executing-plans` task-by-task. Each task is independently
> reviewable.

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
- Preserve the existing 1 MiB capability-body ceiling, 2,048 retained models
  per worker, 256-character model-name ceiling, 60-second admission timeout,
  3,600-second cooldown base with its current 8x exponential cap,
  86,400-second capability TTL, and sixteen named metric slots plus `overflow`
  until an equal or tighter replacement is tested.
- Never increase a limit implicitly from remote input, observed hardware, or a
  worker advertisement.
- Preserve remote consent, HTTPS/trusted-origin validation, no-proxy
  transport, response-size limits, and no-response replay behavior.
- Default `server.py:status()` is cached and non-probing. Only a separately
  named administrator-authorized operation may refresh worker capability
  state or reveal worker detail.
- An external snapshot must pass durable
  `{cluster, issuer, generation, digest}` high-water validation before it can
  change a roster. A registry, DNS, model list, or health probe never creates
  membership authority.
- Do not compose inference membership with compute placement, replication,
  artifact transport, quorum, or takeover ownership.
- Keep operational model-sharding and indefinite-scale capability values
  unavailable.
- Use targeted adds and DCO sign-off for every commit.

---

### Task 1: Add a static roster-capacity contract

**Files:**

- Modify: sonder_runtime/platform/config.py
- Modify: tests/production/test_config.py
- Modify: tests/test_typed_runtime_export.py
- Modify: docs/runbooks/multi-node-ollama.md
- Modify: docs/architecture/generated/runtime-reference.json
- Modify: docs/architecture/generated/runtime-reference.md

**Produces:** Typed values for maximum worker roster, capability-probe
parallelism, capability-probe batch size, and status page size.

- [ ] Write failing typed-configuration tests in
  `tests/production/test_config.py` for default 16, valid 64 and 256,
  below-one rejection, above-256 rejection, a 65-member list under a
  64-worker maximum, and a 257-member list under a 256-worker maximum. Build
  each roster as primary plus distinct workers; assert that a repeated
  canonical worker and a worker canonicalizing to primary are errors instead
  of silent deduplication.

- [ ] Run:

      python -m pytest -q tests/production/test_config.py tests/test_typed_runtime_export.py

  Expect the new static capacity contract to be absent.

- [ ] Add typed TOML and legacy-environment parsing for
  `worker_pool_max_workers`, `worker_capability_probe_parallelism`,
  `worker_capability_probe_batch_size`, and `worker_status_page_size`. The
  maximum includes primary. Normalize primary and workers before counting,
  then reject every duplicate. Defaults are 16 workers, four probes, 32
  workers per probe batch, and 32 status records per page. Valid ranges are 1
  through 256 workers, 1 through 8 probes, and 1 through 128 for batch and
  page size. Preserve typed-over-ambient precedence while preserving injected
  environment maps as the legacy compatibility seam.

- [ ] Document that queue depth remains global, the roster maximum includes
  the primary, duplicate origins are rejected, and a configured maximum is
  not a throughput guarantee.

- [ ] Regenerate the runtime reference after the typed `OllamaConfig` fields
  change. Run `python scripts/generate_documentation_catalogs.py --write`,
  retain only the resulting `runtime-reference.json` and
  `runtime-reference.md` changes in this task, and verify the generator is
  fresh before committing.

- [ ] Run:

      python -m pytest -q tests/production/test_config.py tests/test_typed_runtime_export.py
      python scripts/generate_documentation_catalogs.py --check
      python scripts/check_doc_links.py
      python scripts/check_architecture.py

- [ ] Commit:

      git add -- sonder_runtime/platform/config.py tests/production/test_config.py tests/test_typed_runtime_export.py docs/runbooks/multi-node-ollama.md docs/architecture/generated/runtime-reference.json docs/architecture/generated/runtime-reference.md
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
stable metric-slot lifetime, targeted unknown-capability probing, and a
schema-versioned summary plus cursor-paged worker detail.

- [ ] Write 16, 64, and 256 worker fake-prober tests. Assert that the
  configured maximum rejects the next unique origin in both direct and legacy
  injected-environment construction, the global queue retains its one limit,
  and no more than configured probe parallelism runs at once. Assert that a
  primary/worker duplicate and two normalized duplicate workers fail before a
  scheduler state is created. In the legacy cases, construct the environment
  mapping explicitly; it accepts exactly 64 and 256 total canonical origins
  after `SONDER_OLLAMA_POOL_MAX_WORKERS` is added and never reads process
  environment state.

- [ ] Add a fake-clock blocked-probe test proving that one refresh selects no
  more than the configured batch and that successive refreshes make fair
  progress through all stale workers.

- [ ] Write status tests for deterministic ordering, bounded pages, opaque
  cursors, cursor invalidation after a roster-generation change, and a 65,536
  UTF-8-byte maximum. Use a 2,048-model fixture with hostile overlong names,
  credential-shaped error text, and body-shaped error text. Assert
  `model_count`, no more than eight 128-character model previews, fixed error
  categories, no raw `models` array, no raw error, and no partial record when
  the byte ceiling truncates a page.

- [ ] Write metric-churn tests: first admission binds no more than sixteen
  private stable identities to `w0` through `w15`; a removed/reordered worker
  never gives its named slot to a different identity; the original identity
  retains its slot if it returns; later identities are `overflow`; and no more
  than seventeen metric label values can be emitted in one process lifetime.

- [ ] Write model-specific routing tests for unknown capability state. A
  requested model may target one fair unknown or stale worker, starts at most
  one capability probe outside the admission lock, joins an in-flight probe
  for that worker, and returns the bounded capability-unavailable result when
  the result is missing or fails. A model-less request and a status read start
  no such probe; the current request is never replayed.

- [ ] Run:

      python -m pytest -q tests/test_ollama_pool_scaling.py tests/test_ollama_pool.py tests/test_ollama_pool_distributed.py

  Expect the source-level maximum and full-list status to fail the new tests.

- [ ] Replace the global worker-count admission with a constructor parameter
  and make both `parse_worker_origins` and `from_environment` consume the
  configured maximum before they enforce it. Reject, rather than collapse,
  canonical duplicates at the constructor and typed/legacy boundaries. Pass
  every typed static setting at canonical composition roots
  (`sonder_runtime/__main__.py` serve and MCP paths plus
  `sonder_runtime/bootstrap/app.py`) and retain explicit environment-map
  compatibility behavior.

- [ ] Keep worker metric labels capped at sixteen plus `overflow`. Implement a
  private, bounded identity-to-slot registry with at most sixteen bindings;
  a named slot cannot be reused for a different identity until process restart.
  Do not derive a metric label from origin, member identity, or request data.

- [ ] Implement a persistent stale-worker rotation cursor. Select no more than
  one configured batch and submit no more than configured parallel probes.
  A model-specific targeted probe selects at most one fair unknown/stale
  worker and is single-flight per worker. Perform all network I/O outside the
  pool condition.

- [ ] Implement a cached summary and page output. Preserve current count keys
  during a compatibility period and add schema version, roster generation,
  page cursor, complete flag, omitted count, `serialized_bytes`, model count
  and preview, and a closed error-category enum. The page encoder must stop
  at a complete worker-record boundary at 65,536 UTF-8 bytes; never include an
  endpoint body, credentials, TLS material, workspace path, or raw exception.

- [ ] Run:

      python -m pytest -q tests/test_ollama_pool_scaling.py tests/test_ollama_pool.py tests/test_ollama_pool_distributed.py tests/test_typed_runtime_export.py tests/test_operational_capabilities.py
      python scripts/check_architecture.py
      python scripts/check_error_signals.py

- [ ] Commit:

      git add -- sonder_runtime/adapters/inference/ollama_pool.py sonder_runtime/__main__.py sonder_runtime/bootstrap/app.py tests/test_ollama_pool_scaling.py tests/test_ollama_pool.py tests/test_ollama_pool_distributed.py
      git commit -s -m "feat(inference): admit configurable bounded Ollama rosters"

### Task 3: Show honest capacity in app, REPL, and HTTP

**Files:**

- Modify: server.py
- Modify: sonder_runtime/domain/operational_capabilities.py
- Modify: sonder_runtime/interfaces/http/serve.py
- Modify: sonder_runtime/interfaces/repl/facades/status_model.py
- Modify: app/lib/api.dart
- Modify: app/lib/system_screen.dart
- Modify: tests/test_operational_capabilities.py
- Modify: tests/test_server_helpers.py
- Modify: tests/test_serve_auth.py
- Modify: app/test/api_test.dart
- Modify: app/test/widget_test.dart
- Modify: docs/architecture/generated/runtime-reference.json
- Modify: docs/architecture/generated/runtime-reference.md

**Produces:** A cached non-probing default status, plus an
administrator-authorized cached detail/explicit-refresh operation with bounded
pages and no broadened topology disclosure.

- [ ] Write projection tests for a 64-worker summary. Assert request-level
  pooling is available only while a multi-worker pool accepts work, and model
  sharding and indefinite scale remain unavailable.

- [ ] Write `server.py` tests with a fake pool and fake local Ollama transport.
  The default `status()` must not call `refresh_capabilities`,
  `refresh_inventory`, `/api/tags`, `/api/ps`, DNS, or a worker endpoint; it
  must render cached counts and `not_refreshed`/`unknown` when no cache exists.
  The separately named `ollama_pool_admin_status` operation must check the
  existing administrator boundary before cached detail or
  `refresh=true` work, and an unauthorized call must make zero pool or network
  calls.

- [ ] Write API and Flutter parser tests for schema version 2 truncated pages
  and version 1 count-only compatibility. Cover page size 1 and 128, stale and
  cross-principal cursors, 65,536-byte complete-record truncation,
  administrator-only origin/model previews, closed error categories, and the
  summary's unavailable model-sharding and indefinite-scale values.

- [ ] Run:

      python -m pytest -q tests/test_operational_capabilities.py tests/test_server_helpers.py tests/test_serve_auth.py
      flutter test app/test/api_test.dart app/test/widget_test.dart

- [ ] Change `server.py:status()` to use only the pool's cached summary; do not
  retain a hidden `/api/tags`, `/api/ps`, `refresh_capabilities`, or
  `refresh_inventory` call on its default path. Add
  `ollama_pool_admin_status(token="", refresh=False, cursor="", page_size=32)`:
  it permits direct local-open use but otherwise requires `_admin_require`
  with the `admin` role, pages cached detail without I/O when
  `refresh=false`, and when `refresh=true` runs exactly one configured bounded
  stale refresh. The caller cannot override the configured batch or
  parallelism. Its page has a 65,536 UTF-8-byte ceiling and a generation- and
  principal-bound opaque cursor.

- [ ] Give the HTTP status/detail route the same administrator decision and
  response schema. Render summary counts by default in the REPL and app; only
  show an authorized detail page after an explicit operator action. Do not add
  origins, raw inventory, TLS data, body content, or exception text to the
  default app/REPL state. Add the operation to the HTTP system-operation map
  as an administrator-only operation so the generic `/<tool>` route cannot
  bypass its in-tool check.

- [ ] Regenerate the runtime reference after the new MCP tool and changed
  `server.py` source are present. Run
  `python scripts/generate_documentation_catalogs.py --write`, retain the two
  runtime-reference artifacts, then run the generator in `--check` mode.

- [ ] Run:

      python -m pytest -q tests/test_operational_capabilities.py tests/test_server_helpers.py tests/test_serve_auth.py
      flutter test app/test/api_test.dart app/test/widget_test.dart
      python scripts/generate_documentation_catalogs.py --check
      python scripts/check_architecture.py

- [ ] Commit:

      git add -- server.py sonder_runtime/domain/operational_capabilities.py sonder_runtime/interfaces/http/serve.py sonder_runtime/interfaces/repl/facades/status_model.py app/lib/api.dart app/lib/system_screen.dart tests/test_operational_capabilities.py tests/test_server_helpers.py tests/test_serve_auth.py app/test/api_test.dart app/test/widget_test.dart docs/architecture/generated/runtime-reference.json docs/architecture/generated/runtime-reference.md
      git commit -s -m "feat(status): expose bounded inference roster state"

### Task 4: Define pure membership contracts

**Files:**

- Create: sonder_runtime/domain/inference_membership.py
- Create: sonder_runtime/application/ports/inference_membership.py
- Create: tests/test_inference_membership.py
- Modify: docs/architecture/generated/architecture-map.json
- Modify: docs/architecture/generated/architecture-map.md

**Produces:** Immutable worker advertisements, signed-snapshot digest and
high-water values, a source port, and pure reconciliation results.

- [ ] Write tests for duplicate IDs, malformed identities, non-HTTPS origins,
  invalid protocol version, snapshot size above 4,096, expiry inversion,
  generation rollback, same-generation digest conflict, endpoint replacement,
  member revocation, and a digest that is computed from the verified canonical
  signed envelope rather than an arbitrary parsed object. Use an injected
  timezone-aware clock.

- [ ] Run:

      python -m pytest -q tests/test_inference_membership.py

- [ ] Implement standard-library-only immutable values and reconciliation.
  `MembershipHighWater` has exactly cluster ID, issuer ID, generation, and a
  SHA-256 digest. Reconciliation returns additions, activations, drain
  requests, expirations, and a roster generation. It performs no I/O and
  decides no compute ownership.

- [ ] Regenerate the architecture map with
  `python scripts/generate_documentation_catalogs.py --write`; retain the
  changed `architecture-map.json` and `architecture-map.md` generated files
  for this task, then verify `--check`.

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
- Create: sonder_runtime/application/inference_membership/__init__.py
- Create: sonder_runtime/application/inference_membership/controller.py
- Modify: sonder_runtime/adapters/inference/ollama_pool.py
- Modify: sonder_runtime/bootstrap/app.py
- Create: tests/test_inference_membership_controller.py
- Modify: tests/test_ollama_pool_scaling.py
- Modify: docs/architecture/generated/architecture-map.json
- Modify: docs/architecture/generated/architecture-map.md

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

- [ ] Regenerate the architecture map with
  `python scripts/generate_documentation_catalogs.py --write`; retain the
  changed `architecture-map.json` and `architecture-map.md` files and verify
  `--check`.

- [ ] Run:

      python -m pytest -q tests/test_inference_membership.py tests/test_inference_membership_controller.py tests/test_ollama_pool_scaling.py tests/test_ollama_pool_distributed.py
      python scripts/generate_documentation_catalogs.py --check
      python scripts/check_architecture.py

- [ ] Commit:

      git add -- sonder_runtime/adapters/inference/static_membership.py sonder_runtime/application/inference_membership/__init__.py sonder_runtime/application/inference_membership/controller.py sonder_runtime/adapters/inference/ollama_pool.py sonder_runtime/bootstrap/app.py tests/test_inference_membership_controller.py tests/test_ollama_pool_scaling.py docs/architecture/generated/architecture-map.json docs/architecture/generated/architecture-map.md
      git commit -s -m "feat(inference): reconcile bounded worker rosters"

### Task 6: Add externally authenticated membership

**Files:**

- Create: sonder_runtime/adapters/inference/external_membership.py
- Create: sonder_runtime/adapters/inference/membership_high_water.py
- Modify: sonder_runtime/platform/config.py
- Modify: sonder_runtime/bootstrap/app.py
- Modify: tests/production/test_config.py
- Create: tests/test_external_inference_membership.py
- Create: tests/test_membership_high_water.py
- Modify: tests/test_inference_membership_controller.py
- Modify: docs/runbooks/multi-node-ollama.md
- Modify: docs/architecture/generated/runtime-reference.json
- Modify: docs/architecture/generated/runtime-reference.md
- Modify: docs/architecture/generated/architecture-map.json
- Modify: docs/architecture/generated/architecture-map.md

**Produces:** An opt-in external membership source with durable replay
protection that returns only validated snapshots or privacy-safe failures.

- [ ] Write typed config tests in `tests/production/test_config.py` for an
  external-mode policy that requires an exact cluster ID, issuer ID, protocol,
  source origin, private trust anchor, secret-backed client credential,
  snapshot item and byte caps, and at most 4,096
  `member_id -> {origin,
  tls_server_name_or_ip_san, allowed_cidrs}` entries. Reject a missing policy,
  wildcard or broad DNS policy, duplicate member ID, non-HTTPS origin,
  malformed CIDR, or an endpoint policy beyond
  `membership_snapshot_max_advertisements`.

- [ ] Write adapter tests for redirect rejection, HTTP origin rejection,
  missing or bad client certificate, wrong server identity, untrusted issuer,
  oversized response, invalid signature, expired snapshot, wrong cluster or
  issuer, unallowlisted member identity/origin, and attempts to introduce a
  new trust authority. Inject resolver and connector seams: reject a mixed or
  out-of-CIDR DNS result, assert that a validated DNS result is connected
  directly without a second resolution, and reject SAN/SNI mismatch for both
  DNS and IP-literal policies.

- [ ] Write durable-high-water tests using a temporary state path and a fresh
  controller instance to simulate restart in
  `tests/test_membership_high_water.py`. A smaller generation is rejected;
  equal generation plus a different digest is rejected; equal generation plus
  the same digest is idempotent only before expiry; a greater generation is
  persisted before roster mutation; a failed atomic persistence leaves the
  active roster unchanged. Cluster/issuer changes require an explicit
  migration, never an automatic reset.

- [ ] Run:

      python -m pytest -q tests/production/test_config.py tests/test_external_inference_membership.py tests/test_inference_membership_controller.py

- [ ] Require explicit external mode, cluster ID, issuer ID, protocol version,
  trust anchor, source origin, secret-backed client certificate, refresh
  interval, snapshot item and serialized-byte maxima, and exact bounded member
  endpoint policies. Static remains the default. The registry cannot supply an
  origin, trust anchor, SAN name, CIDR, or credential policy that configuration
  did not already authorize.

- [ ] Implement no-proxy, no-redirect, mutually authenticated HTTPS retrieval
  with reads capped at 1 MiB. Resolve once per connection, reject any answer
  outside the member policy CIDRs, connect to the validated address without a
  second DNS lookup, preserve configured SNI, and verify the exact configured
  DNS/IP SAN under the private trust anchor. Match each advertisement's stable
  identity and canonical origin to its configured policy before returning a
  domain value.

- [ ] Add a durable, atomic, fsync-backed high-water adapter under the runtime
  state path. It stores one `{cluster, issuer, generation, digest}` record and
  exposes compare-and-advance semantics to the controller. Persist a newer
  value before applying its roster; never delete, downgrade, or rotate it on a
  refresh failure.

- [ ] Regenerate runtime references after external configuration fields are
  added with `python scripts/generate_documentation_catalogs.py --write`; keep
  the two runtime-reference and two architecture-map artifacts and confirm
  `--check`.

- [ ] Run:

      python -m pytest -q tests/production/test_config.py tests/test_external_inference_membership.py tests/test_membership_high_water.py tests/test_inference_membership.py tests/test_inference_membership_controller.py tests/test_ollama_pool_scaling.py
      python scripts/generate_documentation_catalogs.py --check
      python scripts/check_architecture.py
      python scripts/check_doc_links.py

- [ ] Commit:

      git add -- sonder_runtime/adapters/inference/external_membership.py sonder_runtime/adapters/inference/membership_high_water.py sonder_runtime/platform/config.py sonder_runtime/bootstrap/app.py tests/production/test_config.py tests/test_external_inference_membership.py tests/test_membership_high_water.py tests/test_inference_membership_controller.py docs/runbooks/multi-node-ollama.md docs/architecture/generated/runtime-reference.json docs/architecture/generated/runtime-reference.md docs/architecture/generated/architecture-map.json docs/architecture/generated/architecture-map.md
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
  expiration, draining, one targeted unknown-capability probe, and
  response-bearing interruption. Assert default `server.py:status()` makes no
  network call, administrator refresh cannot exceed configured batch,
  parallelism, or 65,536 serialized bytes, metric slots do not change identity
  during churn, and sharding and indefinite-scale capabilities remain
  unavailable.

- [ ] Run:

      python -m pytest -q tests/test_inference_membership_acceptance.py

- [ ] Write a disposable-host procedure requiring mutually authenticated TLS
  identities, exact member-origin/SAN/CIDR policies, known model inventories,
  config and high-water revision capture, manual revocation, source outage,
  queue saturation, a DNS-rebinding negative test, cleanup, and an evidence
  record. State that it proves neither model sharding, high-availability
  takeover, automatic failback, nor unbounded scale.

- [ ] Run:

      python -m pytest -q tests/production/test_config.py tests/test_ollama_pool.py tests/test_ollama_pool_distributed.py tests/test_ollama_pool_scaling.py tests/test_inference_membership.py tests/test_inference_membership_controller.py tests/test_external_inference_membership.py tests/test_membership_high_water.py tests/test_inference_membership_acceptance.py tests/test_operational_capabilities.py tests/test_server_helpers.py tests/test_serve_auth.py
      python scripts/check_architecture.py
      python scripts/check_requirement_evidence.py
      python scripts/check_error_signals.py
      python scripts/check_history_privacy.py --json
      python scripts/generate_documentation_catalogs.py --check
      python scripts/check_doc_links.py
      git diff --check

- [ ] Commit:

      git add -- tests/test_inference_membership_acceptance.py tests/test_operational_capabilities.py docs/runbooks/multi-node-ollama.md docs/runbooks/inference-membership-acceptance.md
      git commit -s -m "test(inference): verify bounded scalable worker membership"

## Plan self-review

- Static capacity is independently shippable and preserves defaults.
- Dynamic membership has separate domain, adapter, controller, and integration
  gates; it is not a configuration-only shortcut.
- Every roster and status path is bounded, paged, byte-capped, and testable
  with fakes; default status is cached and non-probing.
- Remote membership cannot bypass endpoint consent, exact member endpoint/TLS
  policy, or durable high-water replay protection, and cannot create authority
  through discovery.
- The plan does not alter compute scheduling, takeover, memory, artifact
  mobility, model sharding, or the unavailable indefinite-scale capability.
