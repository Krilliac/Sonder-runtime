# Security Policy

Lock-owner sidecars and progress deadlines are diagnostic, not authority to
terminate or replay work. A PID alone does not identify a process instance;
unknown or foreign ownership must not be reclaimed as if it were dead. An
effect is not definitively complete until its audit receipt is durable, and
uncertainty fences further mutation even if its persistence attempt fails.
See [stalled-worker recovery](docs/architecture/evidence/STALL-RECOVERY-2026-09-24.md).

> **Contract scope:** this focused contract describes current behavior. Unfinished
> implementation work is tracked in the
> [master implementation specification](docs/architecture/SONDER-MASTER-IMPLEMENTATION-SPEC.md); the
> [behavior status](#behavior-status) table labels what is implemented,
> experimental, proposed, degraded, or unsupported.

## Reporting a vulnerability

Report privately through GitHub's **Report a vulnerability** button on the
[Security tab](https://github.com/Krilliac/Sonder-runtime/security/advisories/new).
That opens a private advisory only the maintainer can read.

Please do not open a public issue for anything that lets an attacker read
files, run code, or reach a model endpoint they should not have. A public
issue is a working exploit that anyone watching the repository can copy
before there is a fix.

Include the version or commit, your platform, whether the runtime was
loopback-only or served, and the smallest reproduction you have. Expect an
acknowledgement within a week.

## What this software does, stated plainly

Sonder Runtime is not a chat wrapper. By design it will, on request:

- **execute code and shell commands** (`run_code`, `script_run`,
  `workspace_run`, `run_project`)
- **read, write, patch, and delete files** (`file_read`, `file_write`, `file_batch_write`, `file_edit`, `text_patch`,
  `file_delete`, `directory_create`, `archive_create`)
- **fetch from the network** (`web_fetch`, `web_search`) and explicitly probe
  loopback services (`local_service_probe`)
- **run unattended** (`autopilot_start`, `master_orchestrate`, `loop`)
- **modify its own source** when self-modification is enabled

Those are the product, not a bug. But it means the trust boundary is the
process itself: anything that can send it a request can, in principle, ask
for any of the above. Treat access to a Sonder endpoint as equivalent to
shell access on the host, and size the sandbox accordingly.

Mitigations that are already in place and worth knowing about:

- Raising the permission mode (to `acceptEdits` or `auto`) needs an attended
  caller: the console's `/mode` or an administrator's
  `POST /v1/permission-mode`. MCP clients and the HTTP chat may only lower it
  (or return from `plan` to `manual`).
- `file_delete` is **dry-run by default** and requires a confirmation string
  that matches one the tool returned; it also enforces root restrictions and
  a developer-authorization check.
- File operations are constrained to configured roots; escaping them takes an
  explicit `extra_roots` or a bypass token.
- Credential stores (`.ssh`, `.aws`, `.azure`, `.gnupg`, `.kube`,
  `.docker/config.json`, `.git/config`, `.netrc`, `.git-credentials`,
  `.pgpass`, OpenSSH key files, `.env*`) are refused by the direct read tools
  even inside an allowed root and even with a developer token or bypass,
  unless an operator-configured file root names the store itself.
- Structured `data_query` reads are bounded and side-effect-free: SQLite uses
  a read-only URI and deny-by-default authorizer, while JSON/JSONL/CSV/TSV use
  exact structured filters and projections without expression evaluation.
- `data_convert` rejects sensitive/control and reparse paths, validates input
  through a no-follow opened handle, and can only atomically create a new
  destination after a complete bounded conversion; it never overwrites.
- Git history inspection (`repo_log`, `repo_show`, `repo_blame`) is read-only, project-bound,
  argv-only, and bounded; it disables parent-repository discovery, pagers,
  external diffs, and text-conversion helpers. Gitfile targets must remain in
  authorized roots, and `repo_show` requires a contained path before returning
  patch content.
- `file_batch_write` never targets secrets/control state or traverses symlinks,
  requires explicit create-versus-overwrite intent, and rolls back completed
  writes on a later failure when restoration remains possible.
- `archive_list` and `archive_extract` accept ZIP/TAR only and prevalidate every
  member under hard entry, byte, ratio, depth, result, and time ceilings.
  Absolute/traversal paths, links/devices, encrypted ZIPs, collisions, nested
  archives, sensitive state, and existing destinations are rejected before a
  staged non-overwriting extraction is promoted.
- `archive_create` prevalidates every explicit input and directory membership,
  refuses sensitive paths, links, special files, caps/escapes, and overwrites,
  then revalidates mutations before atomically publishing a staged ZIP/TAR.
- `text_patch` accepts a narrow unified-diff grammar for explicit relative
  UTF-8 text files. Preview is the default. Apply prevalidates the entire patch,
  rejects deletes/renames/binary/sensitive/link targets, publishes staged files,
  and uses digest-guarded rollback that will not overwrite a concurrent change.
- `workspace_compare` exposes metadata and SHA-256 digests, never file content;
  it fails closed on sensitive/control state, reparse points, special files,
  identity races, or any scan/time/output ceiling.
- `log_inspect` rejects sensitive/control paths and reparse traversal, validates
  the already-open regular-file handle, and uses only fixed host parsers under
  hard file, byte, line, result, output, and time ceilings.
- `artifact_risk_inspect` never renders or executes its input and never returns
  raw file content. It reports bounded static indicators for PDFs, PE/ELF/Mach-O
  executables, scripts, and opaque binaries. Static findings are evidence, not
  a malware verdict; incomplete or unsupported scans are never labelled clean.
- `script_run` and the native MCP `run_script` tool (`ToolExecutorAdapter`)
  apply `SONDER_EXECUTION_RISK_POLICY` through one gate
  (`artifact_risk.run_script_under_policy`) before launching an exact guarded
  script. `run_program`, which launches an argv rather than an inspectable
  script file, is not covered by this gate. The default is `report`;
  operators may choose `deny-high`, `deny-medium`, or `deny-unknown`. A caller may strengthen but cannot weaken
  the configured policy. Under an enforcing `deny-*` mode on Linux, a `.py` or
  `.sh` script is read once through the guarded no-follow handle into a memfd
  sealed against writes, growth and shrinking; the sealed copy is what is
  inspected and what the interpreter runs (Python through a bootstrap that
  reads the inherited descriptor and restores `__file__`, `sys.argv[0]` and
  `sys.path[0]`; bash through `/proc/self/fd/N`, so `$0` is that path).
  Replacing the file after the scan does not change what runs. Scripts over
  the 32 MiB scan budget are refused (`exact_handoff_exceeds_scan_budget`).
  Every other suffix, and every launch on Windows and macOS, still fails closed
  (`exact_execution_handoff_unavailable`). `report` remains advisory. This
  avoids a pathname-swap bypass and is defense in depth, not an OS sandbox:
  the exact handoff covers the entry script's bytes, not files it imports or
  sources, and a shell script that locates siblings through `$0` or
  `BASH_SOURCE` resolves them under `/proc/self/fd/` and will not find them.
- Process inventory and memory-risk inspection are disabled unless the operator
  sets `SONDER_PROCESS_INSPECTION=enabled:bounded-read-only`. The Windows-only
  scanner requests read/query rights for one exact PID and returns only fixed
  indicator names/counts from private readable memory plus aggregate accounting—never command lines, module
  paths, addresses, strings, or memory bytes. It does not enable debug privilege,
  suspend, write, inject, or bypass normal OS access checks. Heuristic matches
  can be wrong and bounded scans can miss data.
- `local_service_probe` remains a direct, explicit MCP operation only. It is
  excluded from agents, repository read-only sessions, loops, and autopilot
  because arbitrary loopback response bodies can contain host-local secrets.
- Cloud tiers are **opt-in**. Local tiers run against loopback Ollama, and a
  remote `OLLAMA_HOST` must be explicitly enabled. A separate opt-in,
  `SONDER_ALLOW_REMOTE_OLLAMA=1` with `SONDER_OLLAMA_WORKERS`, lets ordinary
  local-tier requests be pooled across multiple Ollama hosts (see
  `docs/runbooks/multi-pc-ollama.md`); vision analysis and fanout synthesis
  carry a stricter never-leaves-this-machine contract and are pinned to the
  primary endpoint regardless of pool configuration.
- Lessons are passed through a 20-rule privacy classifier before storage, so
  paths, emails, credentials, private keys, and tokens are not distilled into
  memory.
- Update bundles are staged by a TAR extractor that validates every member
  before writing: traversal, drive-relative/UNC/backslash, alternate-stream,
  reserved-device, control-character, duplicate and case- or
  normalization-colliding names, links, devices, member-count and
  expanded-byte overruns are rejected. Every TAR reader refuses long-name,
  long-link, and PAX metadata records larger than 64 KiB, more than four
  chained metadata records per member, more than 1 MiB of metadata or
  32,768 PAX keys per archive, global PAX headers, and GNU sparse members
  before parsing them. ZIP readers check the declared central-directory entry count before
  opening the archive.
- Self-modification backups, manifests, checksums, the `selfmod_events` audit
  table, and recovery evidence are tamper-evident only. They are written by the
  same OS user as Sonder, so they are not a security boundary against
  `--unrestricted-selfmod`, which can rewrite them consistently; see
  [SELFMOD.md](SELFMOD.md#recovery-and-audit-are-not-a-security-boundary).

The repository also carries a documented historical Git-history privacy gate;
see [the debt evidence record](docs/security/history-privacy-debt.md). Normal
CI rejects new flagged history while tagged publishing fails closed until the
known historical pairs are unreachable.

## Deployment guidance

### Unsafe lab mode (disposable isolated hosts only)

Sonder has a deliberately dangerous model-evaluation override. It is **not an
OS sandbox and provides no containment**. It removes the local model agent and
autopilot host-tool allowlists, project scoping, read-only gate, web/location
gate, and file approval gate so an untrusted or unguarded model can exercise
the host-native tool surface. The shared file-approval bypass also affects every
direct MCP call path that consults it; direct MCP is therefore part of the
blast radius, not an unchanged boundary. Direct tool time/output bounds still
exist, but they are reliability controls, not a security boundary.

Hosted agents remain unable to use local-only or host-data inspection tools,
including artifact and process-risk inspection, even when the unsafe gate is
active. Unsafe mode bypasses only the hosted nested-model restriction. The
operator artifact-execution risk policy and the exact process-inspection
opt-in remain enforced; unsafe mode does not silently rewrite either policy.

The override is off unless `SONDER_UNSAFE_LAB_ACK` exactly equals:

```text
I UNDERSTAND SONDER UNSAFE LAB MODE GIVES MODELS UNRESTRICTED HOST TOOL ACCESS AND I AM RUNNING IN A DISPOSABLE ISOLATED ENVIRONMENT
```

Boolean/truthy, abbreviated, case-changed, or whitespace-modified values fail
closed. Even with the exact acknowledgement, Sonder refuses activation when
`SONDER_HOST` is not loopback or when the process is root/elevated. Activation
appends and flushes a durable JSONL warning under
`$SONDER_HOME/audit/unsafe-lab.jsonl` (override only with
`SONDER_UNSAFE_LAB_AUDIT_PATH`), and status/diagnostics display the mode.
All model-authored subprocess boundaries, including repair campaigns and
self-modification validation/Git calls, receive a scrubbed environment that removes
secret-, credential-, token-, session-, approval-, bypass-, and control-like
variables. That reduces accidental credential inheritance; it does not make
the host safe.

Use this only in a disposable VM or hardened disposable container, under a
dedicated unprivileged account, with no host filesystem mounts, no Docker or
other container-management socket, no device passthrough, no credential
stores, no SSH agent, no cloud metadata access, and restricted network egress.
Take a clean snapshot first and destroy the environment after the test. See
[the unsafe lab runbook](docs/runbooks/unsafe-lab.md).

### Loopback (default, recommended)

The default posture binds Ollama to `127.0.0.1` and keeps the runtime local.
Nothing is exposed to the network. If you are evaluating Sonder, stay here.

### Local development service (`deploy_sonder.sh --serve`)

This convenience mode installs a loopback-only systemd service. It generates a
random `SONDER_API_KEY`, stores it in `/etc/sonder/sonder-local.env` with mode
0600, and prints a `http://127.0.0.1:<port>/v1` URL for local clients. It does
not provide a supported remote deployment and must not be port-forwarded.

Older revisions of this script bound `0.0.0.0` and advertised a plaintext
public URL. Upgrade before using it, remove any firewall rule that exposed port
11435, and rotate the API key if it ever crossed a network in cleartext.

### Remote access

Install the [server-private profile](docs/runbooks/install-server-private.md),
keep its runtime listener on `127.0.0.1`, and follow the
[secure remote access runbook](docs/runbooks/secure-remote-access.md). The
reference nginx configuration terminates TLS, limits requests, and forwards to
the loopback listener. Never expose the runtime port directly: possession of a
valid credential grants access to the runtime's powerful tool surface.

The listener checks the `Host` header before routing (`421
HOST_NOT_ALLOWED`). IP literals, loopback names, the machine's own names and
`[server].allowed_hosts` are always accepted; any other name is accepted only
when the listener requires credentials, and never earns the loopback-peer
exemptions. This stops DNS-rebinding pages from reading the unauthenticated
loopback surface, since rebinding needs an attacker-controlled name and a
listener that answers without credentials. `X-Forwarded-For` is trusted only with
`tls_terminated_by_proxy = true` and a peer inside `trusted_proxy_cidrs`.
See [HTTP API & lifecycle](docs/wiki/05-http-api-and-lifecycle.md).

### Sonder Inference provider

A provider binding to `sonder-inference` sends prompts to a `sonder-infer
serve` process over HTTP. A loopback base URL (`127.0.0.1`, `localhost`,
`::1`; `0.0.0.0` and `::` are rewritten to loopback; other loopback aliases
such as `127.0.0.2` are refused because Inference's Host check rejects them)
needs no consent. Any
other base URL is refused before a byte is sent unless all of these hold:
`SONDER_ALLOW_REMOTE_INFERENCE=1`, an `https://` URL, a
`SONDER_INFERENCE_API_KEY`, and an operation context that allows prompts to
leave the machine. Status probes (health, identity) apply the same URL,
TLS and key checks because they carry the key. The key is in the log redaction
set, is never logged, and is sent only as `Authorization: Bearer`. Inference
traffic ignores `HTTP(S)_PROXY`/`ALL_PROXY` and OS proxy settings, never
follows a redirect (a 3xx is an error, so the key is never re-sent to another
host), and bounds every exchange by one wall-clock budget. `SONDER_INFERENCE_FALLBACK=ollama` sends a request
to local Ollama only when Inference provably never received it. The fallback
call runs with cloud and remote-Ollama consent withdrawn, so Ollama's own
consent gate refuses a hosted (`-cloud`) or remote target even when the
original request allowed cloud; the fallback cannot widen where a prompt
goes. See [the provider reference](docs/architecture/sonder-inference-provider.md).

### Observatory telemetry and ecosystem routes

`/.well-known/sonder-telemetry`, `/v1/observability/events` (SSE and NDJSON)
and `/v1/sonder/ecosystem` require administrator authorization exactly as
`/v1/observability/trace` does; in local-open loopback mode that is every
loopback caller. They export content-free events only: prompts, responses,
summaries and provider payloads are never exported (`text_capture` is
`none`), and bridged event-sink codes are an allowlist with sanitized
detail. Browsers are admitted by exact-match allowlists: the route-scoped
`SONDER_OBSERVATORY_ORIGINS` applies to these three GET routes only, while
the global `SONDER_CORS_ORIGINS` also admits that origin to every admin route,
so an Observatory origin belongs in the route-scoped list. After the
listener's `Host` check, these routes on a loopback bind also refuse a `Host`
that is not `127.0.0.1`, `localhost` or `[::1]` (403 `forbidden_host`) unless
a TLS-terminating proxy is declared. The event ring and the subscriber count
are bounded (429 beyond the cap), and `SONDER_OBSERVATORY_EXPORT=0` removes
the telemetry routes. See [the telemetry reference](docs/architecture/observatory-telemetry.md).

## Supported versions

This is a single-maintainer project. Fixes land on `main`; there are no
backported release branches. Track `main`.

## Scope

In scope: sandbox escapes from the file and execution tools, authentication
bypass on the served API, credential or memory leakage into logs, lessons, or
exports, and privilege escalation through the elevation broker.

Out of scope: the fact that the tools execute code when asked (see above),
issues that require an attacker who already has the API key or local shell,
and anything in the upstream base models' output. Model output is not a
security boundary — do not rely on it to refuse anything.

## Behavior status

Labels follow the [documentation status vocabulary](docs/architecture/DOCUMENT-AUTHORITY-INDEX.md#documentation-status-vocabulary). Unfinished
implementation work is tracked only in the
[master implementation specification](docs/architecture/SONDER-MASTER-IMPLEMENTATION-SPEC.md).

| Behavior | Status | Boundary |
|---|---|---|
| Guarded file, archive, patch, data, and Git inspection tools | Implemented | Constrained to configured roots and the per-tool ceilings described above. |
| `script_run` execution-risk policy in `report` mode | Implemented | Default mode; advisory, and not an OS sandbox. |
| `script_run` / native `run_script` enforcing `deny-high`, `deny-medium`, or `deny-unknown` modes | Degraded | Linux `.py`/`.sh` scripts run the sealed memfd copy that was inspected; every other runner, and every launch on Windows and macOS, fails closed because no exact inspected-bytes-to-interpreter handoff exists there. |
| Process inventory and memory-risk inspection | Implemented | Off unless `SONDER_PROCESS_INSPECTION=enabled:bounded-read-only`; Windows only. |
| Process memory-risk inspection on non-Windows hosts | Unsupported | The bounded scanner is Windows-only. |
| Unsafe lab mode | Experimental | Exact acknowledgement, loopback-only, and unprivileged; disposable isolated hosts only, and never an OS sandbox. |
| Sonder Inference provider on a loopback endpoint | Implemented | No consent needed; `sonder-inference` bindings only, see the Sonder Inference provider section. |
| Remote Sonder Inference endpoint | Experimental | Refused unless `SONDER_ALLOW_REMOTE_INFERENCE=1`, `https://`, an API key and a cloud-allowed operation context all hold; Inference itself serves no TLS, so a TLS-terminating proxy is required. |
| Observatory telemetry and ecosystem routes | Implemented | Admin-authorized, content-free, exact-match origin allowlists; see the Observatory telemetry section. |
| Exposing the runtime port directly to a network | Unsupported | Remote access requires the server-private profile behind a TLS reverse proxy. |
| Scoped credential handles for every tool instead of injected raw secrets | Proposed | SEC-001; an opaque-handle provider boundary exists but is not yet the end-to-end tool contract. |
