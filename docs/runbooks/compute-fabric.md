# Private Compute Fabric

Sonder can place one bounded, cataloged job on the local host or an explicitly
configured private compute node. It is an orchestration plane, not a remote
shell, shared-memory layer, hypervisor, storage server, or inference gateway.

For deployment profiles and control-state ownership capabilities, see
[deployment topology](deployment-topology.md). Reference pages for the inventory
and refresh surface and for ownership takeover:
[compute-indexed-inventory.md](compute-indexed-inventory.md) and
[takeover-readiness.md](takeover-readiness.md).

Volatile facts below (defaults, which capabilities are live, route names) were
checked against the code on 2026-11-20. Every key is listed in
[Configuration reference](#configuration-reference).

## Contents

1. [Consent boundaries](#consent-boundaries)
2. [Setup in order](#setup-in-order)
3. [Worker setup](#worker-setup)
4. [Front a worker with a TLS proxy](#front-a-worker-with-a-tls-proxy)
5. [Worker catalog](#worker-catalog)
6. [Worker capacity admission](#worker-capacity-admission)
7. [Controller setup](#controller-setup)
8. [Windows and WSL workers](#windows-and-wsl-workers)
9. [What can be placed today](#what-can-be-placed-today)
10. [Using it from MCP](#using-it-from-mcp)
11. [Dispatch invariants](#dispatch-invariants)
12. [Metrics](#metrics)
13. [Configuration reference](#configuration-reference)
14. [Diagnosis and recovery](#diagnosis-and-recovery)
15. [Rollback](#rollback)

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

## Setup in order

A fabric has one **controller** (the Sonder process that decides placement and
holds your MCP client) and one or more **workers** (ordinary Sonder `serve`
processes that run catalog jobs). Do the worker first, check it locally, put the
TLS proxy in front, then configure the controller.

1. Pick a stable node id for each worker, for example `linux-node`
   ([identity rules](#worker-setup)).
2. On the worker: write the TOML and secrets file, define the catalog, start
   `serve` on loopback ([Worker setup](#worker-setup),
   [Worker catalog](#worker-catalog)).
3. On the worker: put a TLS reverse proxy in front
   ([Front a worker with a TLS proxy](#front-a-worker-with-a-tls-proxy)).
4. On the controller: add `[compute].allow_remote` and one `[[compute.nodes]]`
   entry per worker, trust the proxy's CA ([Controller setup](#controller-setup)).
5. Check what the worker can really run
   ([What can be placed today](#what-can-be-placed-today)), then submit a first
   job ([Using it from MCP](#using-it-from-mcp)).

## Worker setup

A compute worker is the ordinary `serve` process: there is no separate worker
binary or mode. Any Sonder process whose HTTP listener is reachable answers the
`/v1/compute/*` routes. The worker needs **no** `[compute].allow_remote`; that
key gates only the controller side and the `/v1/compute/nodes/refresh` route.

Prerequisites: a working Sonder install (see
[setup-start-here.md](setup-start-here.md)), the external programs your catalog
names, a private network path to the controller (private direct link, VPN or
private LAN; never the Internet), and a TLS reverse proxy (next section).

### Identity

- `[compute].node_id` on the worker is reported as the worker id in every job
  receipt and as `node_id` in every snapshot.
- The controller rejects a snapshot whose `node_id` differs from the matching
  `[[compute.nodes]].id`, and rejects a receipt from a different worker id.
- `node_id` defaults to `local`. An unconfigured worker therefore never
  validates against a controller node called anything else.
- Ids match `[A-Za-z0-9][A-Za-z0-9._-]{0,127}`. The controller's own `node_id`
  must differ from every remote node id.

### Steps

1. Choose the home directory. Default: `%LOCALAPPDATA%\sonder` on Windows,
   `$XDG_DATA_HOME/sonder` or `~/.local/share/sonder` on Linux. `SONDER_HOME`
   (or `[state].home`) overrides it.
2. Generate an API key of at least 24 characters and put it in the secrets
   file, never in TOML:

   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

   Save it as `SONDER_API_KEY=<the key>` in `<sonder-home>/sonder.env` (Linux:
   `chmod 600`). The same key must be configured on the controller.
3. Write `<sonder-home>/sonder.toml`. Minimal worker (Linux paths; a Windows
   variant is under [Windows and WSL workers](#windows-and-wsl-workers)):

   ```toml
   [server]
   host = "127.0.0.1"
   port = 11435
   tls_terminated_by_proxy = true
   auth_mode = "api-key"

   [state]
   workspace_roots = ["/srv/work/sonder-runtime"]

   [compute]
   node_id = "linux-node"

   [[compute.jobs]]
   id = "sonder-pytest"
   workload = "test"
   program = "/srv/sonder/.venv/bin/python"
   fixed_args = ["-m", "pytest"]
   argument_policy = "relative-paths-and-test-selectors"
   workspace_mappings = ["sonder-runtime"]
   ```

   Keep `[server].host = "127.0.0.1"`: the TLS proxy runs on the same host and
   reaches it over loopback. A non-loopback bind is accepted by the loader only
   with `tls_terminated_by_proxy = true` **and** an API key of at least 24
   characters, and the reference topology never needs it.
4. Check the configuration without binding a port. A worker with no Ollama can
   skip that probe:

   ```bash
   python -m sonder_runtime preflight --config <sonder-home>/sonder.toml --secrets <sonder-home>/sonder.env --skip-ollama
   ```

   Expected: a report with every required check ok and exit code `0`
   (`1` when a required check fails; `2` for a configuration error that names
   the key).
5. Start it. `--config` and `--secrets` default to `<sonder-home>/sonder.toml`
   and `<sonder-home>/sonder.env` when those files exist; `SONDER_CONFIG` and
   `SONDER_SECRETS` also work. The optional positional port overrides
   `[server].port`.

   ```bash
   python -m sonder_runtime serve --config <sonder-home>/sonder.toml --secrets <sonder-home>/sonder.env --skip-ollama
   ```

   The launchers `sonder-serve.cmd` / `sonder-serve.sh` pass their arguments to
   the same command, but first run `sonder_headless.py engine` and stop with a
   non-zero exit when no local inference engine is usable. A compute-only worker with no Ollama should call
   `python -m sonder_runtime serve` directly, as above.
6. Verify on the worker (the plain-HTTP loopback port, before any proxy). Use a
   second shell, since `serve` holds the first, and export the key there
   (`export SONDER_API_KEY=<the key>`): `sonder.env` is not read by your shell.

   ```bash
   curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:11435/v1/compute/snapshot
   # 401: auth is on. A 200 here means the listener is local-open: stop, fix auth, do not front it.
   curl -s -H "Authorization: Bearer $SONDER_API_KEY" http://127.0.0.1:11435/v1/compute/snapshot
   # 200 JSON with node_id "linux-node", health, live_capabilities, advertised_workloads, resources, active_jobs
   ```

   `GET /v1/compute/snapshot` accepts any authorized credential. Job routes
   (submit, cancel, status, by-idempotency, artifact bytes) are admin-only:
   the API key in `api-key` mode. Keep the worker in `auth_mode = "api-key"`
   (the default): the controller sends only the API key as a Bearer token, and
   in `both` mode a bare API key is not an administrator.

### Workspace mapping rule

A job names a workspace by mapping name, not by path. On a worker the names are
the **basenames of the resolved** `[state].workspace_roots` entries, plus
`default`, which is always present and points at the first root (or the
process working directory when no root is set). Rules:

- Two roots with the same basename fail configuration.
- Every name in a `[[compute.jobs]].workspace_mappings` must exist locally, or
  startup fails with `... workspace_mappings is not available locally: [...];
  configure matching [state].workspace_roots`.
- The controller's per-node `[[compute.nodes]].workspace_mappings` is a free
  list of names. A request for a name the node does not list is rejected with
  `workspace_unavailable`. Keep the two lists identical by hand.
- Environment alternative: `SONDER_FILE_ROOTS` (roots separated by the OS path
  separator). `SONDER_DEFAULT_WORKSPACE_ROOT` does not help: it sets only
  `[state].default_workspace_root`, never a mapping name.

### Spool directory requirement

Job artifacts are staged in `<tempdir>/sonder-compute-artifacts` (and inputs in
`<tempdir>/sonder-compute-inputs`), where `<tempdir>` is the process temp
directory (the first of `TMPDIR`, `TEMP`, `TMP` that is set to a usable directory, on every platform, else the platform default). The location is not
configurable. The directory must be private to the Sonder user: owner-only on
POSIX, an exclusive ACL (owner and SYSTEM full control) on Windows. Anything
else fails with `private compute artifact spool is unsafe`. Run the worker as
a dedicated account whose temp directory nobody else can write.

Published artifacts are kept for 14 days by default
(`artifact_retention_seconds`). Retention is enforced lazily: expired spool
directories are removed only when another job's artifacts are next collected
(when a job reaches a terminal state). A stopped worker never prunes, so
artifacts stay on disk until a later job runs or the operator removes them.

## Front a worker with a TLS proxy

Sonder's stdlib server does not terminate TLS, and the controller accepts only
`https://` origins. Install nginx or Caddy **on the worker host** and keep
Sonder on loopback. The full procedure, with the reference Caddyfile and nginx
file, the Host and `X-Forwarded-For` behaviour and the verification checklist,
is in [secure-remote-access.md](secure-remote-access.md#setup-with-caddy)
(nginx: [setup-with-nginx](secure-remote-access.md#setup-with-nginx)). Follow it
with these worker-specific points:

- Forward `/v1/*`. The compute routes are `/v1/compute/snapshot`,
  `/v1/compute/jobs`, `/v1/compute/jobs/{id}`,
  `/v1/compute/jobs/{id}/cancel`, `/v1/compute/jobs/by-idempotency/{key}` and
  `/v1/compute/jobs/{id}/artifacts/{name}`. The reference proxies already
  forward `/v1/*` and `/live` only; do not add other paths.
- Request bodies are limited by `[server].max_request_bytes` (default 1 MiB,
  1 byte to 16 MiB). A job envelope is far smaller; keep the proxy limit equal
  to it.
- An artifact download can be up to 64 MiB. Do not set a proxy response or
  read timeout shorter than a transfer needs; the nginx reference uses
  `proxy_read_timeout 300s`. Caddy's `reverse_proxy` is understood to have no
  response deadline by default (Caddy documentation; not run here).
- Run the proxy on the same host. If it runs elsewhere, the worker needs a
  non-loopback `[server].host` (API key of at least 24 characters,
  `tls_terminated_by_proxy = true`, host firewall allowing only the proxy) and
  the proxy's address in `[server].trusted_proxy_cidrs`; the default and the
  `X-Forwarded-For` rule are in
  [secure-remote-access.md](secure-remote-access.md).
- The certificate must name the host or IP the controller uses in
  `[[compute.nodes]].origin` (SAN). With Caddy `tls internal` or another private
  CA the controller must trust that CA ([Controller setup](#controller-setup)).
- Never front a listener that has no API key: behind a loopback proxy every
  remote caller would look like a loopback administrator.

The reference Caddyfile and nginx file listen on 443. If this host is also an
Ollama worker, both reference Caddyfiles want 443: use one Caddy config with
two ports ([combined host](remote-ollama-caddy.md#one-host-that-is-both-the-ollama-worker-and-a-compute-node)).
The examples below use port 8443, so set the Caddy site address to `worker.example.internal:8443` (or
the nginx `listen` to 8443), or use `:443` in the origin. The controller
requires an explicit port in `origin`. Export the key first
(`export SONDER_API_KEY=<the key>`); `sonder.env` is read by Sonder, not by your
shell.

Verify through the proxy from the controller host (replace the origin):

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://worker.example.internal:8443/v1/compute/snapshot   # 401
curl -s -H "Authorization: Bearer $SONDER_API_KEY" https://worker.example.internal:8443/v1/compute/snapshot   # 200, node_id matches
```

A certificate error here is the same error the controller will log; fix trust
before configuring the controller.

## Worker catalog

The worker owns executable paths, fixed arguments, environment allow-lists,
and workspace roots. The controller sends only a catalog entry ID, bounded
arguments, an allow-listed environment projection, and a relative workspace
path.

```toml
[server]
host = "127.0.0.1"
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

A catalog entry only makes a program *runnable*. Whether the controller will
*place* that workload on this worker depends on the live capabilities the worker
reports: the `encode` and `render` entries above are accepted by the catalog but
are rejected at placement today. See
[What can be placed today](#what-can-be-placed-today).

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
use the authenticated HTTP route. At most 256 artifacts are published per job.
These job artifacts are separate from the scoped, resumable
[artifact transfer](../artifact-transfer.md) between private stores.

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

The budget is bound to an opaque SHA-256 physical-host fingerprint in jobs.db.
When no fingerprint is supplied by an embedding application, the SQLite worker
adapter derives one from the local Windows machine GUID or Linux machine-id,
with the OS family included so Windows and WSL do not silently share a fence.
A changed fingerprint is rejected even when `worker_host_id` is unchanged.
`reconcile_capacity(limit=...)` marks only expired, undispatched leases as
`expired`; dispatched rows remain occupied until cleanup proof. The bounded
`list_capacity(include_released=True)` view omits reservation tokens and is
intended for operator reconciliation. The fingerprint is an operational fence,
not hardware attestation; cloned machine identities remain outside this claim.

The authority covers catalog job RAM accounting and concurrent job slots only.
It does not reserve inference VRAM, CPU cores, disk bandwidth, or RAM used by
unrelated applications. Use the same durable database and physical host identity
for participating processes on one host. Independently configured Windows/WSL
workers or different PCs do not share a safe budget merely because their labels
match. No common cross-OS authority or inference resource service is provided here.
Preserve jobs.db on rollback; unresolved dispatched rows require verified process
cleanup, not deletion of rows to make capacity appear free.

Whole-job placement requests may carry a bounded `priority` (-100..100) and a
`max_queue_depth` admission ceiling. The scheduler treats the worker-reported
`active_jobs` value as queue pressure when that ceiling is present. The
application-owned `PlacementQueue` provides a process-local pre-placement queue
with a fixed maximum depth, deterministic priority/age/request ordering,
duplicate replay, bounded expiry, and redacted explanations. It does not replace
durable worker capacity or claim a cross-process queue.

## Controller setup

The controller is any Sonder process that holds the MCP client and decides
placement. It always has a local node (all workloads except inference, all
capabilities configured, workspace names from its own `[state].workspace_roots`
plus `default`) and may also have up to 15 remote nodes.

Prerequisites: each worker answers `GET /v1/compute/snapshot` through its TLS
proxy ([verified above](#front-a-worker-with-a-tls-proxy)), and the controller
has the same `SONDER_API_KEY` (at least 24 characters) in its secrets file.
The key is never TOML or part of an origin URL.

1. Add to the controller's `sonder.toml` (the worker's `node_id` must equal
   the `id` here, and the controller's own `node_id` must differ from it):

   ```toml
   [compute]
   allow_remote = true
   node_id = "main-windows"
   snapshot_ttl_seconds = 30
   probe_timeout_ms = 2000

   [[compute.nodes]]
   id = "linux-node"
   origin = "https://worker.example.internal:8443"
   workloads = ["build", "test", "index", "analysis", "fuzz", "embedding",
                "training", "render", "encode", "service", "container", "storage"]
   capabilities = ["cpu", "ram", "cuda", "cmake", "clang", "clangd",
                   "clang-tidy", "pytest", "sccache", "embeddings", "qlora",
                   "ffmpeg", "blender", "docker", "storage", "database"]
   workspace_mappings = ["sonder-runtime", "game"]
   preference_weight = 2.0
   ```

   `origin` must be `https://host:port` with an explicit port and no
   credentials, path, query or fragment. A `[[compute.nodes]]` entry without
   `[compute].allow_remote = true` fails configuration, as does an API key
   shorter than 24 characters.
2. Configuration is maximum authority. Each worker snapshot narrows it to the
   capabilities that are live and the workloads currently advertised. A name in
   `capabilities` is never proof that the node can do it; list only what you
   have installed and what the worker really reports
   ([What can be placed today](#what-can-be-placed-today)).
3. Trust the proxy's CA on the controller (below), then restart the controller.
4. Verify: the control-plane `compute_fabric` section lists the node as
   configured, live, healthy and fresh. From a shell,
   `GET /v1/compute/nodes` on the controller (administrator credential) shows
   the same inventory; see [compute-indexed-inventory.md](compute-indexed-inventory.md).
   Right after a restart the worker shows observed=false and unknown health:
   the control-plane `compute_fabric` section probes only the local node, and
   `GET /v1/compute/nodes` never probes. A remote node is first observed during
   a placement or a `POST /v1/compute/nodes/refresh` (admin credential, JSON
   body `{}`, needs `allow_remote`). Run the refresh, then read
   `GET /v1/compute/nodes` and the `compute_fabric` section: the worker should
   now be live, healthy and fresh. The gauge `sonder_compute_nodes` is set only
   when a placement runs (unset before the first one) and counts the
   controller's own local node too, so with one worker `healthy` is `2`.

### TLS trust on the controller

There is no `[compute]` CA setting. Compute calls use the default certificate
context: the operating system trust store, plus whatever `SSL_CERT_FILE` /
`SSL_CERT_DIR` point at in the **controller process's** environment. A change
to either variable is read on use, but a running process's environment cannot
be changed from outside, so in practice only edits to the `SSL_CERT_FILE` file
(picked up without a restart) and OS store changes (within 5 minutes) take
effect; otherwise restart the controller.

- Public certificate (ACME) on the worker proxy: nothing to do.
- Private CA or Caddy `tls internal`: install the CA root into the controller's
  OS trust store, or start the controller with `SSL_CERT_FILE` set to a PEM
  bundle that contains it. Verification is always on (certificate required and
  host name checked); there is no insecure switch.
- The same host also serving Ollama to this controller's pool: the pool lane
  trusts through `[ollama].ca_bundle` instead, and `SSL_CERT_FILE` doubles as
  that bundle when it is unset. One Caddy with two ports, and both trust paths,
  are in
  [remote-ollama-caddy.md](remote-ollama-caddy.md#one-host-that-is-both-the-ollama-worker-and-a-compute-node)
  and [Coordinator trust for both lanes](remote-ollama-caddy.md#coordinator-trust-for-both-lanes).

Proxy caveat (inferred from the Python standard library, **not run**): compute
requests are sent through `urllib`'s opener, which honours `HTTPS_PROXY` /
`NO_PROXY`. If the controller process has `HTTPS_PROXY` set, compute calls may be
rerouted through it. Add the worker host to `NO_PROXY` or unset the variable.
The artifact peer-transfer client, by contrast, disables proxies.

Symptoms: `probe-failed:*` evidence on the node plus a certificate error in the
controller log means trust or SAN is wrong; `snapshot node identity does not match configured
identity` means the ids differ; a `401`/`403` means the key differs or the worker
is not in `api-key` mode.

## Windows and WSL workers

Windows and Linux/WSL workers run the same `serve` command; only the paths,
containment and prerequisites differ.

Windows worker, minimal TOML (use single-quoted TOML strings for backslash
paths):

```toml
[server]
host = "127.0.0.1"
port = 11435
tls_terminated_by_proxy = true

[state]
workspace_roots = ['C:\work\sonder-runtime']

[compute]
node_id = "win-worker"

[[compute.jobs]]
id = "sonder-pytest"
workload = "test"
program = 'C:\sonder\venv\Scripts\python.exe'
fixed_args = ["-m", "pytest"]
argument_policy = "relative-paths-and-test-selectors"
workspace_mappings = ["sonder-runtime"]
```

Run `serve` in one PowerShell window (it stays in the foreground):

```powershell
python -m sonder_runtime serve --config "$env:LOCALAPPDATA\sonder\sonder.toml" --secrets "$env:LOCALAPPDATA\sonder\sonder.env" --skip-ollama
```

In a second window, set the key (`sonder.env` is not read by your shell) and
query it:

```powershell
$env:SONDER_API_KEY = "<the key>"
Invoke-RestMethod -Headers @{ Authorization = "Bearer $env:SONDER_API_KEY" } http://127.0.0.1:11435/v1/compute/snapshot
# node_id "win-worker"
```

- **Containment.** Windows jobs run inside a Job Object. A `program` must be an
  absolute path (Windows or POSIX style, at most 4096 characters), so a bare
  `python` is rejected at startup.
- **WSL / Linux containment.** Linux and WSL workers need `systemd-run` and
  `systemctl` on `PATH`, or launch fails closed. A WSL distribution without a
  running systemd is expected to have no usable user manager (inferred, not
  run). Non-root runs use the systemd user manager; set
  `SONDER_COMPUTE_SYSTEMD_USER` to `0`/`1`/`true`/`false`/`yes`/`no` to force
  the manager. Any other value is an error; unset means user manager unless
  root.
- **Separate homes.** A Windows worker and a WSL worker on the same PC are two
  workers: give each its own `node_id`, `[state].home` and `jobs.db`
  (`SONDER_JOBS_DB` overrides the path only when no `[state].home` is set;
  `SONDER_HOME`/`SONDER_STATE_HOME` also set `[state].home`, so it is ignored then too).
  They derive different host fingerprints (Windows machine GUID versus Linux
  machine-id, with the OS family in the digest) and a shared `jobs.db` is
  rejected as a changed fingerprint. Never copy a `jobs.db` between OS
  instances.
- **Network and firewall.** WSL networking differs from the Windows host; the
  TLS proxy must listen where the controller can reach it. For the private
  link, address plan, worker firewall rules and rollback on Windows see
  [multi-node-windows-private-link.md](multi-node-windows-private-link.md)
  sections
  [6 (Restrict the worker firewall)](multi-node-windows-private-link.md#6-restrict-the-worker-firewall) and
  [7 (Validate from the primary PC)](multi-node-windows-private-link.md#7-validate-from-the-primary-pc).
  That runbook is written for inference workers; the link, SSH and firewall
  parts apply unchanged to compute workers on the proxy port.

## What can be placed today

Known gap, checked 2026-11-20: the controller schedules from a worker's
**live** capabilities, and the live set is produced by a fixed probe list, not
by your catalog. A catalog entry for a program outside that list is accepted at
startup but the workload is never placed. The scheduler requires
`configured capabilities` (the controller's `[[compute.nodes]].capabilities`)
**and** `live capabilities` (worker snapshot).

Live capabilities come from the worker's local snapshot, which maps executables
found on `PATH` by `platform/environment_probe.py` (a fixed list of toolchain
and specialist tool names) plus hardware probes:

| Live today | Source |
|---|---|
| `cmake`, `clang` (`clang`, `clang++`), `msvc` (`cl`, `msbuild`), `docker`, `pytest`, `sccache` | executable found on `PATH` |
| `cpu`, `ram`, `cuda`, `storage` | hardware/disk probes (`cuda` only when CUDA is available) |

Never live, because their executables are not in the probe list: `blender`,
`clang-tidy`, `clangd`, `distcc`, `ffmpeg`, `llama-server` (`llamacpp`),
`ollama`, `podman`; and `qlora`, `embeddings`, `database`, `kvm`, which have no
probe at all.

What that means per workload (capability profile in
`domain/compute_profiles.py`):

| Workload | Needs | Placeable today |
|---|---|---|
| `build` | `cpu` and one of `cmake`, `msvc`, `clang` | yes |
| `test`, `fuzz` | `cpu` | yes |
| `analysis` | `clang-tidy` or `clang` | yes, through `clang` only |
| `training` | `ram` and `cuda` or `qlora` | only through `cuda` |
| `container` | `docker` or `podman`, plus digest-bound input artifacts | yes, through `docker` |
| `service` | none | yes |
| `storage` | `storage` | yes |
| `index` | `clangd` | no: rejected `missing_any_capability` |
| `encode` | `ffmpeg` | no: rejected `missing_any_capability` |
| `render` | `blender` | no: rejected `missing_capability` |
| `embedding` | `embeddings`, `ollama` or `llamacpp` | no: rejected `missing_any_capability` |

Do not rely on `index`, `encode`, `render` or `embedding` workloads, nor on
`qlora`/`embeddings`/`database` capabilities, until the probe list grows. Check
the live list on your worker at any time:

```bash
curl -s -H "Authorization: Bearer $SONDER_API_KEY" https://worker.example.internal:8443/v1/compute/snapshot
# "live_capabilities": [...] is exactly what the scheduler can use
```

The snapshot advertises every workload except `inference`. Only the controller's
`[[compute.nodes]].workloads` narrows that list; the worker catalog does not.
A request is also rejected for health, freshness (`snapshot_ttl_seconds`),
workspace (`workspace_unavailable`), queue depth, and RAM/disk/VRAM/load floors.

## Using it from MCP

Submission is exposed on the **native** MCP transport only:

```bash
python -m sonder_runtime mcp --native --config <sonder-home>/sonder.toml --secrets <sonder-home>/sonder.env
```

| Tool | Permission class | Purpose |
|---|---|---|
| `compute_submit` | execution | place and start one catalog job |
| `compute_cancel` | mutation | cancel a placed job (`controller_job_id`, `reason`) |
| `compute_status` | safe | read a placed job (`controller_job_id`) |
| `compute_artifact_fetch` | safe | read one published artifact (`controller_job_id`, `name`, `max_bytes` up to 98304 = 96 KiB) |

All four go through the runtime permission policy; a denial returns the error
`permission_denied` with the text `compute host control denied by runtime
permission policy`.

`compute_submit` arguments:

- Required: `request_id`, `workload`, `catalog_entry_id`, `workspace_mapping`,
  `allow_remote`. `request_id` becomes the `controller_job_id` you later pass
  to status, cancel and fetch.
- Defaults: `idempotency_key` = `request_id`; `relative_cwd` `.`;
  `deadline_seconds` 300 (1 to 86400); `idempotent` false; `allow_local_fallback`
  false (true requires `allow_remote` true); `placement_policy` unset. Also
  accepted: `arguments` (up to 64), `environment` (up to 32 entries),
  `input_artifacts` (up to 64 `{name, size_bytes, sha256}`).
- `workload` is one of `analysis build container embedding encode fuzz index
  render service storage test training` (no `inference`). `container` requires
  `input_artifacts`.

Placement semantics:

- `allow_remote=false`: runs on the controller's local node only.
- `allow_remote=true` with no `placement_policy`: remote nodes first; the local
  node only when `allow_local_fallback=true` and the remote search found
  nothing.
- `placement_policy`: `local-only`, `prefer-remote`, `rank-all` (local and
  remote ranked together).
- Remote nodes are probed only when `[compute].allow_remote=true`. Per-call
  `allow_remote` never overrides the global gate.

Result (JSON text in `output`): `node_id`, `remote_job_id`, `controller_job_id`,
`idempotency_key`, `state`, `request_sha256`, `artifacts` (`name`, `size_bytes`,
`mime_type`, `sha256`), `output_preview`, `output_watermark`, `output_truncated`.
`state` is one of `pending`, `claimed`, `running`, `paused`, `cancelling`,
`cancellation_requested`, `succeeded`, `failed`, `cancelled`, `interrupted`.

Artifact bytes: `compute_artifact_fetch` returns at most 96 KiB (base64). Larger
accepted artifacts (up to 64 MiB) are fetched with
`GET /v1/compute/jobs/{remote_job_id}/artifacts/{name}` using the API key; the
controller verifies `Content-Length`, `Content-Type` and
`X-Sonder-Artifact-Sha256` against the receipt.

First job, smoke test: with the `sonder-pytest` entry from
[Worker catalog](#worker-catalog) (it allows `-q`) call `compute_submit` with
`{"request_id":"first-test-1","workload":"test","catalog_entry_id":"sonder-pytest","workspace_mapping":"sonder-runtime","allow_remote":true,"placement_policy":"prefer-remote","arguments":["-q"]}`.
Expected: `node_id` is the worker's id and `state` moves to `succeeded`
(`compute_status` with `controller_job_id` `first-test-1`). A rejection names a
reason code such as `workspace_unavailable` or `missing_capability`
([Diagnosis and recovery](#diagnosis-and-recovery)).

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

Controller placement identity, selected node, request digest, idempotency key,
and remote job identity are stored in the durable job registry. After a Sonder
restart, status and cancellation rehydrate that record and reconcile an
ambiguous idempotent dispatch by its idempotency key before retrying.

If submission times out after the request may have been sent, an idempotent
request is reconciled once through its idempotency key. It is not blindly
resubmitted. A non-idempotent request is never retried automatically. The job
transport socket timeout is 5 seconds per call (the client accepts values above 0 up to 30;
the runtime uses the default).

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

Not every workload is placeable today; see
[What can be placed today](#what-can-be-placed-today).

## Metrics

Fixed-cardinality Prometheus metrics, scraped from the loopback `/metrics` on
the controller:

| Metric | Labels | Meaning |
|---|---|---|
| `sonder_compute_nodes` | `state` = `configured`, `live`, `healthy`, `unhealthy`, `stale` | node counts |
| `sonder_compute_active_jobs` | none | worker-reported active jobs |
| `sonder_compute_placements_total` | `route` = `local`, `remote` or `other` | successful placements |
| `sonder_compute_placement_rejections_total` | `reason` (bounded reason code) | rejected placements |

Raw node names, workspace paths, commands, and artifact names are never metric
labels.

## Configuration reference

The one home for compute keys; the wiki configuration page links here. Values
checked against `sonder_runtime/platform/config.py` on 2026-11-20. Unknown keys
fail configuration. All keys below are TOML; those with an environment variable
list it. Byte values are integers.

### `[compute]`

| Key | Default | Range / rule | Environment |
|---|---|---|---|
| `allow_remote` | `false` | boolean; gates the controller and `/v1/compute/nodes/refresh` | `SONDER_ALLOW_REMOTE_COMPUTE` |
| `node_id` | `local` | identity; worker id and snapshot `node_id`; must differ from every remote node id | |
| `snapshot_ttl_seconds` | `30` | 1..3600 | `SONDER_COMPUTE_SNAPSHOT_TTL_SECONDS` |
| `probe_timeout_ms` | `2000` | 1..30000 | `SONDER_COMPUTE_PROBE_TIMEOUT_MS` |
| `worker_host_id` | `local` | identity; names this host's capacity authority | |
| `worker_memory_budget_bytes` | unset | 0..2^50. Unset = measured, exclusive admission; `0` disables dispatch; TOML only | |
| `worker_max_jobs` | `1` | 1..1024 | |
| `worker_reservation_seconds` | `30` | 1..300 | |
| `max_input_staging_bytes` | 8 GiB | 1..2^40; must not exceed `max_input_spool_bytes` | |
| `max_input_spool_bytes` | 32 GiB | 1..2^40 | |
| `max_artifact_spool_bytes` | 32 GiB | 1..2^40 | |
| `max_artifact_spool_jobs` | `1024` | 1..4096 | |
| `artifact_retention_seconds` | 14 days (1209600) | 1..30 days | |
| `min_disk_headroom_bytes` | 512 MiB | 1..2^40 | |

### `[[compute.nodes]]` (controller side, at most 15)

| Key | Default | Rule |
|---|---|---|
| `id` | required | identity; must equal the worker's `[compute].node_id`; unique |
| `origin` | required | `https://host:port`, explicit port, no credentials, path, query or fragment |
| `workloads` | required, non-empty | subset of `build test index analysis fuzz embedding training render encode service container storage` (the validator also accepts `inference`; the MCP tool never submits it) |
| `capabilities` | empty | subset of `cpu ram cuda ollama llamacpp docker podman kvm cmake msvc clang clangd clang-tidy sccache distcc pytest embeddings qlora ffmpeg blender storage database`; only the live ones are usable ([gap](#what-can-be-placed-today)) |
| `workspace_mappings` | empty | free list of identities |
| `preference_weight` | `0.0` | -100..100 |

Remote entries need `[compute].allow_remote = true` and `SONDER_API_KEY` of at
least 24 characters.

### `[[compute.jobs]]` (worker catalog, unique `id`)

| Key | Default | Rule |
|---|---|---|
| `id` | required | identity |
| `workload` | required | a workload above (not `inference`) |
| `program` | required | absolute Windows or POSIX path, at most 4096 characters |
| `fixed_args` | empty | at most 64 values of at most 4096 characters |
| `argument_policy` | `none` | `none`, `bounded` or `relative-paths-and-test-selectors` |
| `environment_allowlist` | empty | at most 32 variable names |
| `workspace_mappings` | empty | each must exist locally ([rule](#workspace-mapping-rule)) |
| `allowed_flags`, `allowed_bounded_options`, `allowed_relative_path_options` | empty | option names like `-q` or `--name`; an option may be in only one group |
| `memory_limit_bytes` | unset | 1..2^50; OS process cap |
| `memory_reservation_bytes` | unset | 1..2^50; admission demand; omitted consumes the whole budget |
| `artifact_paths` | empty | at most 256 relative paths; no absolute path or `..` |

### Other variables used by a fabric

| Variable | Effect |
|---|---|
| `SONDER_API_KEY` | secrets file or environment; at least 24 characters; same on controller and worker |
| `SONDER_JOBS_DB` | path of `jobs.db` when no `[state].home` is configured (`SONDER_HOME`/`SONDER_STATE_HOME` set it too); otherwise `<sonder-home>/jobs.db` |
| `SONDER_COMPUTE_SYSTEMD_USER` | Linux/WSL: `0`/`1`/`true`/`false`/`yes`/`no`; unset = user manager unless root |
| `SONDER_FILE_ROOTS` | workspace roots ([rule](#workspace-mapping-rule)) |
| `SSL_CERT_FILE`, `SSL_CERT_DIR` | controller-side CA trust ([TLS trust](#tls-trust-on-the-controller)) |

Fixed limits (not configurable): artifact size 64 MiB, 256 artifacts per job,
snapshot response 64 KiB, MCP artifact fetch 96 KiB, spool root
`<tempdir>/sonder-compute-artifacts`.

For `[deployment]` `pooled-pair` rules (exactly one local node and one distinct
peer) see [deployment topology](deployment-topology.md).

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

| Symptom | Cause and fix |
|---|---|
| Startup error `... workspace_mappings is not available locally ...; configure matching [state].workspace_roots` | A catalog mapping has no matching root basename on the worker. Add the root or fix the name. |
| `[compute].nodes[0] requires [compute].allow_remote=true` | Controller TOML lacks `allow_remote = true`. |
| `[compute].node_id must differ from remote node identities` | Controller and a remote node share an id. |
| `snapshot node identity does not match configured identity` | Worker `[compute].node_id` (default `local`) differs from the controller's `[[compute.nodes]].id`. |
| `probe-failed:*`, certificate error in the log | Controller does not trust the proxy CA, or the certificate does not name the origin's host. |
| `401` | Wrong or missing `SONDER_API_KEY`. |
| `403` / `administrator authorization is required` | Worker is not in `api-key` mode, or the credential is not the API key. |
| Rejection `workspace_unavailable` | The node's `[[compute.nodes]].workspace_mappings` lacks the requested name. |
| Rejection `missing_capability` / `missing_any_capability` | The needed capability is not live ([gap](#what-can-be-placed-today)). |
| `private compute artifact spool is unsafe` | The temp directory's `sonder-compute-artifacts` is shared or has foreign permissions. Use a private account/temp dir. |
| Linux job launch fails closed | `systemd-run` or `systemctl` missing, or no user manager. |
| `permission_denied` from an MCP tool | Runtime permission mode or operator rule blocks the compute tool. |

Do not repair availability by disabling TLS/authentication, broadening the
catalog to a shell, exposing ports, installing packages automatically,
escalating privilege, moving private data, or deleting an existing workspace.

## Rollback

1. Controller: remove the `[[compute.nodes]]` entries (or set
   `[compute].allow_remote = false`) and restart. Local compute continues.
2. Worker: stop the `serve` process, then remove or disable the proxy site
   ([rollback steps](secure-remote-access.md#rollback)) and close the firewall
   opening.
3. Keep `jobs.db`. Unresolved dispatched reservations need verified process
   cleanup; deleting rows to make capacity appear free is not a rollback.
4. Published artifacts stay in `<tempdir>/sonder-compute-artifacts` after the
   worker stops: retention is enforced only when a later job collects
   artifacts. Remove the directory by hand after stopping `serve` if the
   artifacts are private.
