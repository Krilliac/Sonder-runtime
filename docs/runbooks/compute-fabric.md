# Private Compute Fabric

Sonder can place one bounded, cataloged job on the local host or an explicitly
configured private compute node. It is an orchestration plane, not a remote
shell, shared-memory layer, hypervisor, storage server, or inference gateway.

For deployment profiles and control-state ownership capabilities, see
[deployment topology](deployment-topology.md).

## Consent boundaries

The three networked execution lanes are independent:

| Lane | Default | Consent |
|---|---|---|
| Local compute | enabled | existing local execution/file permissions |
| Private-node compute | disabled | `[compute].allow_remote=true` plus `WorkloadRequest.allow_remote=true` |
| Hosted/cloud models | disabled | `[features].cloud=true` / `SONDER_ALLOW_CLOUD=1` |

Remote compute consent never enables cloud models. Cloud consent never admits a
compute node. Remote Ollama has a third, inference-only consent gate under
`[ollama].allow_remote`; inference remains owned by the model gateway and is
never sent through the generic compute-job API.

## Worker capacity admission

Catalog process jobs reserve capacity in the worker's existing jobs.db before
launch. The default samples local available RAM immediately before admission and
runs one catalog job at a time. Missing or older-than-five-second RAM observations
reject admission with an availability reason. This measurement is an observation,
not a guarantee against unrelated processes allocating RAM afterward.

For a fixed operator budget set `[compute].worker_memory_budget_bytes` to a positive
byte count and `worker_max_jobs` to a slot limit. Explicit zero disables dispatch;
omitting the budget selects measured, exclusive admission regardless of the slot
setting. `worker_host_id` defaults to `local` and identifies this authority.
A configured budget represents accounting capacity, not measured free RAM.
Each catalog entry may set `memory_reservation_bytes`; omitted demand consumes
the whole budget. `memory_limit_bytes` remains the separate OS process cap and
is never silently treated as demand. Set both when the workload needs both.

Admission leases default to 30 seconds (`worker_reservation_seconds`, 1..300).
An unconsumed lease may expire; a dispatched reservation remains occupied until
the process owner proves cleanup. Restart, timeout, interrupted state, and an
ambiguous launch exception do not free that reservation. Active legacy catalog
jobs without admission records block new admissions until resolved. Terminal
legacy jobs predate this accounting and are not retroactively reserved.

The authority covers catalog job RAM accounting and concurrent job slots only.
It does not reserve inference VRAM, CPU cores, disk bandwidth, or RAM used by
unrelated applications. Use the same durable database and physical host identity
for participating processes on one host. Independently configured Windows/WSL
workers or different PCs do not share a safe budget merely because their labels
match. No common cross-OS authority or inference resource service is provided here.
Preserve jobs.db on rollback; unresolved dispatched rows require verified process
cleanup, not deletion of rows to make capacity appear free.

## Network and identity

Use a private direct link, VPN, or private LAN. The configured node origin must
be an HTTPS origin with an explicit port. Sonder's stdlib server does not
terminate TLS; put a private TLS reverse proxy in front of it and configure the
server's existing proxy declaration. Restrict the listener with the host
firewall. Do not publish it to the Internet.

Both controller and worker use the same `SONDER_API_KEY`, supplied through the
secrets file or process environment, never TOML or an origin URL. A remote-node
configuration is rejected unless the API key is at least 24 characters.

## Worker configuration

The worker owns executable paths, fixed arguments, environment allow-lists,
and workspace roots. The controller sends only a catalog entry ID, bounded
arguments, an allow-listed environment projection, and a relative workspace
path.

```toml
[server]
host = "0.0.0.0"
port = 11435
tls_terminated_by_proxy = true

[state]
home = "/var/lib/sonder"
workspace_roots = ["/srv/work/sonder-runtime", "/srv/work/game"]

[compute]
node_id = "linux-node"

[[compute.jobs]]
id = "sonder-pytest"
workload = "test"
program = "/srv/sonder/.venv/bin/python"
fixed_args = ["-m", "pytest"]
argument_policy = "relative-paths-and-test-selectors"
environment_allowlist = ["PYTEST_ADDOPTS"]
workspace_mappings = ["sonder-runtime"]
allowed_flags = ["-q"]
allowed_bounded_options = ["--color"]
allowed_relative_path_options = ["--basetemp"]
memory_limit_bytes = 2147483648
artifact_paths = ["reports/junit.xml"]

[[compute.jobs]]
id = "sonder-cmake-build"
workload = "build"
program = "/usr/bin/cmake"
fixed_args = ["--build"]
argument_policy = "bounded"
workspace_mappings = ["sonder-runtime"]

[[compute.jobs]]
id = "game-clang-tidy"
workload = "analysis"
program = "/usr/bin/clang-tidy"
argument_policy = "relative-paths-and-test-selectors"
workspace_mappings = ["game"]

[[compute.jobs]]
id = "media-ffmpeg"
workload = "encode"
program = "/usr/bin/ffmpeg"
argument_policy = "relative-paths-and-test-selectors"
workspace_mappings = ["game"]

[[compute.jobs]]
id = "asset-blender"
workload = "render"
program = "/usr/bin/blender"
fixed_args = ["--background"]
argument_policy = "relative-paths-and-test-selectors"
workspace_mappings = ["game"]
```

Use absolute programs. The operator installs CMake, compilers, pytest,
clangd/clang-tidy, fuzzers, FFmpeg, Blender, CUDA/QLoRA dependencies,
Docker/Podman, databases, and cache services. Merely finding an executable does
not prove it is healthy, GPU-enabled, licensed, or ready for a particular job.

Controller-supplied options are denied unless the worker catalog names them.
Flags take no value, bounded options use `--name=value` with a scalar value,
and relative-path options use `--name=relative/path`. Put invariant options in
`fixed_args`; controller absolute paths and workspace-escaping symlinks are
rejected.

`memory_limit_bytes` is worker-owned catalog policy. Compute jobs use an
OS-owned aggregate job boundary: a Windows Job Object or a transient Linux
systemd scope. Linux workers require `systemd-run` and `systemctl`; launch fails
closed when those tools are unavailable. The scope applies `TasksMax` to the
owner plus its bounded descendants and, when configured, `MemoryMax` to the
whole cgroup. Non-root workers use the user manager by default; set
`SONDER_COMPUTE_SYSTEMD_USER=false` only when the service deliberately runs
against the system manager. Generic non-compute process jobs retain their
existing per-process memory limiter. The controller cannot raise or remove a
worker-owned bound.

Sonder persists the scope identity and reclaims it after worker restart. A job
cannot report terminal success while the scope is still populated. Deadline or
operator cancellation remains `cancellation_requested` until the process tree
is proven empty; forced scope cleanup produces a cancelled result rather than
turning an incompletely contained run into success.

Container work must declare digest-bound input artifacts. Before launch, the
worker resolves each declared input beneath the selected workspace and checks
its exact byte count and SHA-256. The input list is part of the request digest.
This prevents a controller from referring to mutable or workspace-escaping
container inputs.

Worker catalogs may name fixed `artifact_paths`. When a job reaches a terminal
state, Sonder opens each present regular file, hashes that exact handle, and
publishes size, MIME type, and SHA-256 only when the handle and path remain the
same stable file throughout the snapshot. Artifacts above 64 MiB are rejected.
Bytes are available only through the authenticated admin route
`GET /v1/compute/jobs/{remote_job_id}/artifacts/{encoded_name}`; the server
revalidates the file against the published receipt immediately before delivery.
Controllers enforce the same 64 MiB transport ceiling and perform the same
length, type, header-digest, and body-digest checks. Native MCP can retrieve
artifacts up to 96 KiB with `compute_artifact_fetch`; larger accepted artifacts
use the authenticated HTTP route.

## Native MCP controller tools

The native MCP catalog exposes `compute_submit`, `compute_status`,
`compute_cancel`, and `compute_artifact_fetch`. `compute_submit` requires an explicit `allow_remote` boolean
for every workload. Setting it does not override `[compute].allow_remote`; both
the per-call and global gates must authorize private-node placement. Inference
is excluded and remains owned by the model gateway. Permission modes classify
submission as execution and cancellation as mutation; neither operation falls
through to an unclassified default.

Controller placement identity, selected node, request digest, idempotency key,
and remote job identity are stored in the durable job registry. After a Sonder
restart, status and cancellation rehydrate that record and reconcile an
ambiguous idempotent dispatch by its idempotency key before retrying.

## Controller configuration

```toml
[compute]
allow_remote = true
node_id = "main-windows"
snapshot_ttl_seconds = 30
probe_timeout_ms = 2000

[[compute.nodes]]
id = "linux-node"
origin = "https://linux-node.private:8443"
workloads = ["build", "test", "index", "analysis", "fuzz", "embedding",
             "training", "render", "encode", "service", "container", "storage"]
capabilities = ["cpu", "ram", "cuda", "cmake", "clang", "clangd",
                "clang-tidy", "pytest", "sccache", "embeddings", "qlora",
                "ffmpeg", "blender", "docker", "storage", "database"]
workspace_mappings = ["sonder-runtime", "game"]
preference_weight = 2.0
```

Configuration is maximum authority. Each worker snapshot narrows it to live
capabilities and currently advertised workloads. A configured name is never
treated as proof that a node is reachable.

Sonder exports fixed-cardinality Prometheus metrics for configured/live/
healthy/unhealthy/stale node counts, worker-reported active jobs, successful
local/remote placements, and bounded placement-rejection reasons. Raw node
names, workspace paths, commands, and artifact names are never metric labels.

## Dispatch invariants

Before submitting, the controller obtains a fresh authenticated snapshot and
checks health, workload allow-list, live capabilities, workspace mapping,
RAM/disk/VRAM headroom, load, active jobs, and per-workload remote consent.
The scheduler places the entire job on one host. It does not tensor-shard a
model, page remote RAM, or transparently move a workspace.

Each submission binds its material fields to SHA-256. The worker returns its
identity, the remote job ID, controller ID, idempotency key, request digest,
state, and bounded artifact receipts. The controller rejects a mismatched
worker identity or digest.

If submission times out after the request may have been sent, an idempotent
request is reconciled once through its idempotency key. It is not blindly
resubmitted. A non-idempotent request is never retried automatically.

Local fallback is off by default. It occurs only when the request both enables
remote compute and explicitly allows local fallback, and the independently
measured local snapshot satisfies every constraint.

## Workload guidance

- `build`, `test`: whole build trees or suites; preserve incremental caches.
- `index`, `analysis`, `fuzz`: background CPU/RAM work with explicit deadlines
  for fuzzing.
- `embedding`, `training`: keep inference routing separate; require measured
  accelerator/provider readiness for GPU work.
- `render`, `encode`: Blender and FFmpeg catalog entries with relative assets.
- `service`, `container`: only predefined private services/images; bind images
  and inputs by digest and do not expose public ports implicitly.
- `storage`: explicit cache/artifact operations with hashes; no automatic
  migration, overwrite, deletion, or NAS administration.

## Diagnosis and recovery

1. Inspect the control-plane `compute_fabric` section. Confirm the node is
   configured, remote compute is enabled, and its snapshot is healthy/fresh.
2. On the node, verify the TLS proxy, API key, Sonder process, firewall, clock,
   workspace root, catalog entry, and external executable.
3. A failed probe marks the node unhealthy immediately and records a bounded
   `probe-failed:*` error separately. The last successful worker timestamp,
   resources, capabilities, and evidence reference remain visible for diagnosis;
   stale or unknown measurements are still ineligible.
4. For an ambiguous submit, query the idempotency key before taking any action.
5. Cancellation reports `cancelled` only when cleanup is quiescent; otherwise
   it remains `cancellation_requested` and needs operator follow-up.
6. Verify output and artifact hashes on the owning workstation before applying
   results or recording a grounded learning outcome.

Do not repair availability by disabling TLS/authentication, broadening the
catalog to a shell, exposing ports, installing packages automatically,
escalating privilege, moving private data, or deleting an existing workspace.
