# Sonder Compute Fabric Design

**Date:** 2026-08-31
**Status:** Accepted for implementation under the operator's standing authorization
**Source brief:** the shared "Offload Node Computation" conversation, reconciled against the current Sonder repository

## Outcome

Sonder will treat the workstation and one or more private nodes as a small,
capability-aware compute fabric. Existing model routing remains the authority
for inference. A new compute-fabric layer will place whole non-inference jobs
on an eligible healthy node, retain stable job and artifact identity, and fail
closed when health, capability, workspace, or authorization evidence is absent.

The first production slice supports these workload classes through one common
contract:

- build and test jobs;
- indexing, static analysis, and fuzzing jobs;
- embedding and training jobs;
- rendering and media-encoding jobs;
- bounded service and container jobs;
- storage/cache maintenance jobs.

This is not transparent remote memory, arbitrary remote shell, or cross-host
tensor sharding. It is request-level placement of explicit, bounded work.

## Feature audit

| Shared-conversation capability | Current Sonder state | Evidence | Implementation decision |
|---|---|---|---|
| Ollama and llama.cpp inference | Present | typed Ollama pool, provider dispatch, OpenAI-compatible llama.cpp gateway | Reuse; project its nodes into fabric status without replacing routing |
| Multi-PC inference and load routing | Present | `ollama_pool.py`, circuit breakers, model inventory, least-inflight routing, remote-consent/TLS gates | Reuse as the inference-specialized scheduler |
| Remote agents/subagents | Partial | bounded A2A card/JSON-RPC/task identity; no outbound delegated transport | Share node identity and receipt rules; transport remains a separate later A2A slice |
| Parallel compilation / remote builds | Missing as remote service | local process jobs and compiler-cache inspection exist | Add `build` workload placement; adapters invoke an allow-listed worker job API |
| Dedicated CI/test runner | Missing as remote service | local test tools and durable job registry exist | Add `test` workload placement using the same worker API |
| Embeddings/RAG/background indexing | Present locally, partial remotely | embedding model, memory store, retrieval, backfill | Add `embedding` and `index` placement profiles; keep memory ownership local unless explicitly configured |
| LoRA/QLoRA training | Present locally, partial remotely | `qlora_train.py`, adaptive hardware planner, gated promotion | Add `training` placement; promotion remains local-owner authority |
| clangd/static analysis/fuzzing | Partial | symbol index, toolchain probes, bounded decoder fuzz harness | Add `index`, `analysis`, and `fuzz` profiles; tools must be worker-advertised |
| Game/server and database hosting | Missing as fabric-managed service | no durable remote service lease | Add a constrained `service` profile and lease identity; no public exposure automation |
| Docker/Podman/KVM | Partial | guarded container/world contracts; transport-free reference provider | Add container capability telemetry and a bounded remote job transport; KVM remains capability metadata, not an implicit launcher |
| NAS/model/build cache | Partial | storage probes, artifact/file transfer, local cache inspection | Add storage/cache capabilities and placement constraints; no automatic data migration |
| Blender/FFmpeg jobs | Missing as named workloads | generic process execution exists | Add `render` and `encode` profiles, tool capability requirements, and artifact receipts |
| Hardware-aware placement | Partial | local hardware probe, model VRAM scheduler, local fleet capacity | Add shared node snapshots and a pure whole-job scheduler |
| Route away while main GPU is busy | Present for modality inference only | live free-VRAM scheduler and Ollama worker pool | Generalize eligibility/pressure scoring for non-inference jobs; do not alter provider routing semantics |
| Node daemon with CPU/RAM/GPU/storage/load/model telemetry | Missing as unified contract | local probes and control-plane health sections exist separately | Add a bounded snapshot contract and authenticated HTTP projection |
| Node discovery/benchmarking/resource pool | Missing as unified registry | static Ollama worker list only | Add explicit static registry plus TTL observations; no unauthenticated LAN discovery |
| Global resource scheduler | Missing | no generic cross-host placement authority | Add pure scheduler with constraints, scoring, fallback, and evidence |

## Architectural boundaries

### Existing authorities retained

- The model gateway and Ollama pool own inference dispatch.
- `JobRegistry` owns durable local job identity and lifecycle.
- execution-world contracts own isolation claims and refuse to infer security.
- the permission gate owns mutation and host-control authorization.
- the runtime configuration loader owns startup configuration.
- the HTTP server's existing authentication and TLS-proxy rules own network exposure.
- training promotion remains a local, attended, validation-gated operation.

The compute fabric composes these authorities. It does not create a second job
ledger, permission system, or inference router.

### New domain model

`sonder_runtime/domain/compute_fabric.py` will define pure immutable values:

- `WorkloadKind`: `build`, `test`, `index`, `analysis`, `fuzz`, `embedding`,
  `training`, `render`, `encode`, `service`, `container`, `storage`, `inference`;
- `ComputeCapability`: bounded vocabulary such as `cpu`, `ram`, `cuda`,
  `ollama`, `llamacpp`, `docker`, `podman`, `kvm`, `cmake`, `msvc`, `clang`,
  `clangd`, `sccache`, `pytest`, `ffmpeg`, and `blender`;
- `NodeResources`: measured totals/free values, utilization, and monotonic
  observation time;
- `ComputeNode`: stable configured identity, HTTPS origin, allowed workload
  kinds, static capabilities, workspace mappings, and local/remote truth;
- `NodeSnapshot`: one node plus live resource/capability observations, model
  inventory, active jobs, latency, health, and evidence time;
- `WorkloadRequest`: requirements, resource floors, locality/privacy,
  preferred/avoided nodes, fallback policy, idempotency, and deadline;
- `PlacementDecision`: selected node or a fail-closed rejection, ranked eligible
  candidates, reasons, and observation digests.

Constructors reject unknown enum values, negative resources, stale/naive times,
non-HTTPS remote origins, inline credentials, empty identities, and ambiguous
workspace mappings.

### Node registry

`ComputeNodeRegistry` is a process-local read-through registry backed by typed
startup configuration. Configuration establishes identity and authority;
telemetry can only narrow eligibility, never add an unconfigured node or widen
its workload/capability allow-list.

The registry:

- admits at most 15 remote nodes in the first slice;
- keeps the local node explicit;
- stores the latest independently probed snapshot with a TTL;
- returns immutable snapshots in deterministic node-id order;
- preserves last-known evidence for diagnostics but marks stale nodes
  ineligible;
- never performs LAN broadcast discovery.

### Telemetry

The local snapshot collector combines existing bounded probes for CPU, memory,
GPU/VRAM, disk, toolchain/provider presence, and running-job count. Unknown
values remain unknown. Executable presence is not called readiness. GPU vendor
is not called CUDA capability. Configured storage is not called writable until
the storage probe supplies evidence.

Remote telemetry uses an authenticated `GET /v1/compute/snapshot` request to a
configured HTTPS Sonder origin. The returned node id must exactly match the
configured id. Responses are byte-, time-, field-, and vocabulary-bounded.
The client rejects redirects, inline credentials, insecure origins, unknown
capabilities, future timestamps, stale observations, and identity mismatch.

### Placement

`ComputePlacementScheduler` is pure. It first applies hard constraints:

1. configured and healthy;
2. fresh observation;
3. workload kind allowed by both static config and live advertisement;
4. required capabilities present;
5. privacy/locality satisfied;
6. workspace mapping available when files are needed;
7. minimum free RAM/disk/VRAM and maximum load satisfied;
8. deadline and queue admission feasible when known.

It then scores eligible nodes using normalized headroom, load, active jobs,
latency, warm model/tool affinity, and operator preference. Selection is
deterministic with node id as the final tie-break. Unknown measurements never
earn positive headroom. The decision carries every candidate's acceptance or
rejection reason so routing is explainable.

Inference requests are projected for visibility but delegated to the existing
model gateway/Ollama pool. The generic scheduler must not select an inference
transport itself.

### Remote job contract

The first transport is a bounded worker-job API on the existing authenticated
Sonder HTTP service:

- `GET /v1/compute/snapshot` — read-only node observation;
- `POST /v1/compute/jobs` — submit a catalog-bound job request;
- `GET /v1/compute/jobs/{id}` — bounded state/output/artifact receipts;
- `POST /v1/compute/jobs/{id}/cancel` — explicit cancellation.

The controller sends a `RemoteJobEnvelope`, never a command string. It contains:

- controller job id and idempotency key;
- workload kind and worker catalog entry id;
- bounded argv values validated by that catalog entry;
- configured workspace mapping id plus relative working directory;
- explicit environment keys from the catalog allow-list;
- deadline and resource limits;
- request SHA-256.

The worker resolves the catalog entry locally. The controller cannot select an
arbitrary executable or absolute path. Catalog entries are operator-owned
configuration such as `cmake-build`, `pytest`, `clang-tidy`, `ffmpeg-transcode`,
or `blender-render`. Unknown entries and paths escaping a configured workspace
are rejected before job creation.

Worker responses carry worker id, remote job id, request digest, lifecycle
state, bounded output watermark/preview, and artifact receipts. Artifact
receipts contain relative name, size, MIME type, and SHA-256; content transfer
continues to use the existing bounded file/artifact mechanisms.

Ambiguous failures are not replayed unless the job is explicitly idempotent and
the same idempotency key/request digest can be queried first. Cancellation is
best-effort and reports cleanup truth; it never claims a process stopped without
worker evidence.

### Workload profiles

Profiles are data, not custom transports:

| Profile | Required capabilities | Default resource behavior |
|---|---|---|
| build | configured compiler/build tool | CPU and RAM headroom; cache affinity bonus |
| test | configured test runner | CPU/RAM headroom; workspace required |
| index | clangd or configured indexer | background priority; RAM/disk floor |
| analysis | configured analyzer | CPU/RAM; workspace required |
| fuzz | configured harness | hard deadline and process limits |
| embedding | embedding provider/model | model affinity and RAM/VRAM floor |
| training | training stack plus accelerator when required | attended, isolated from serving pressure |
| render | blender | GPU/CPU headroom plus artifact space |
| encode | ffmpeg | CPU/GPU capability plus artifact space |
| service | configured service catalog entry | lease/health contract; no public exposure |
| container | docker or podman | digest-bound image and guarded policy |
| storage | configured storage/cache operation | disk headroom and explicit mapping |

`distcc`, `sccache`, CI runners, databases, game servers, KVM, and NAS remain
operator-installed services/capabilities. Sonder detects and schedules them; it
does not silently install packages, expose ports, create VMs, or migrate data.

## Configuration

The typed configuration gains a `[compute]` section and repeatable node/catalog
tables. Defaults preserve current behavior: compute fabric enabled for local
read-only status, remote execution disabled, and no remote nodes.

Representative shape:

```toml
[compute]
snapshot_ttl_seconds = 30
probe_timeout_ms = 2000
allow_remote = false

[[compute.nodes]]
id = "linux-node"
origin = "https://10.20.0.2:8443"
workloads = ["build", "test", "index", "training", "render", "encode"]
capabilities = ["cpu", "ram", "cuda", "cmake", "clang", "sccache", "ffmpeg"]
workspace_mappings = ["sonder"]

[[compute.jobs]]
id = "pytest"
workload = "test"
program = "/opt/sonder/venv/bin/python"
fixed_args = ["-m", "pytest"]
argument_policy = "relative-paths-and-test-selectors"
environment_allowlist = ["PYTEST_ADDOPTS"]
workspace_mappings = ["sonder"]
```

Secrets are references or injected server credentials, never serialized into
node snapshots, placement decisions, logs, manifests, or config diagnostics.

## Observability

Control-plane status gains a `compute_fabric` section with configured/healthy/
stale node counts, active placements, and rejection counters. Metrics use only
bounded labels: node id, workload kind, decision, and reason code. Request text,
argv values, paths, endpoints, secrets, model prompts, and artifact names are
never metric labels.

Every placement records a receipt containing request digest, snapshot digest,
selected node, reasons, and local/remote truth. This receipt is diagnostic
evidence, not proof that the remote process completed.

## Failure behavior

- No eligible node: reject with structured reasons or use local fallback only
  when the request explicitly allows it and the local node is independently
  eligible.
- Probe failure: retain last evidence for display, mark the node unhealthy or
  stale, and do not schedule new work there.
- Mid-submit timeout: query by idempotency key before retrying; otherwise surface
  an ambiguous result and require reconciliation.
- Worker restart: worker job registry remains authoritative; controller polls by
  stable remote job id.
- Lost controller: worker deadline and catalog resource bounds still apply.
- Digest/identity mismatch: reject response, open the worker circuit, retain
  security-safe evidence.
- Local GPU pressure: non-inference scheduler deprioritizes GPU work; inference
  routing remains owned by the Ollama/model gateway.

## Implementation slices

1. Pure domain contracts, workload vocabulary, scheduler, and exhaustive unit
   tests for validation, eligibility, determinism, staleness, and scoring.
2. Typed configuration, static registry, local snapshot collector, and
   read-only status projection.
3. Bounded HTTP snapshot endpoint/client with authentication inherited from the
   existing server and strict response validation.
4. Catalog-bound remote job envelope/client/worker facade, idempotency and
   artifact receipts, initially exercised through in-memory transport tests.
5. Runtime composition, control-plane visibility, runbook, and focused
   integration/security tests.
6. Operational profiles for build/test/index/analysis/fuzz/training/render/
   encode/service/container/storage, without automatic package installation.

## Acceptance criteria

- Existing installations with no `[compute]` configuration behave unchanged.
- A stale, unhealthy, insecure, mismatched, under-resourced, or unauthorized
  node is never selected.
- The same request and snapshots produce the same placement decision.
- No API accepts an arbitrary remote command or absolute controller path.
- Remote origins require HTTPS, contain no inline credentials, and are explicitly
  enabled.
- Inference routing tests continue to pass unchanged.
- Worker receipts bind node id, job id, idempotency key, and request digest.
- Build, test, index, analysis, fuzz, training, render, encode, service,
  container, and storage profiles all exercise the common scheduler in tests.
- Documentation distinguishes present runtime capabilities from external
  operator-installed tools and services.

## Explicit non-goals

- transparent remote RAM or swap;
- tensor/model sharding over ordinary Ethernet;
- unauthenticated discovery or plaintext remote control;
- arbitrary shell-as-a-service;
- automatic SSH key, firewall, package, VM, public-port, NAS, or database setup;
- automatic data/model movement or deletion;
- claiming container/VM isolation without independently supplied evidence.
