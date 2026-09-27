# Security Model

Sonder is private-first. Its guarantees are **host-enforced**, independent
of the model — an uncensored or "abliterated" model changes what it will
*discuss*, never what the runtime will *let it do*.

## Network posture

- **Loopback by default.** The HTTP server binds `127.0.0.1`. A
  non-loopback bind is **rejected before the socket opens** unless both
  `tls_terminated_by_proxy = true` and a strong (`≥24` char) `SONDER_API_KEY`
  are set. The reference deployment keeps loopback behind a TLS reverse
  proxy regardless ([secure-remote-access](../runbooks/secure-remote-access.md)).
- **Consent gates**, each independent and default-off: cloud/hosted models
  (`SONDER_ALLOW_CLOUD`), web tools (`SONDER_WEB_TOOLS`), remote Ollama
  (`SONDER_ALLOW_REMOTE_OLLAMA`), private-node whole-job compute
  (`SONDER_ALLOW_REMOTE_COMPUTE`, additionally requiring per-workload
  `allow_remote`), approximate location, model reasoning
  (`SONDER_EXPOSE_REASONING`), and private chain-of-thought
  (`SONDER_ALLOW_PRIVATE_COT`). Runtime policy can never turn any of these on.
- **Remote Sonder Inference needs four things at once.** A provider binding
  to `sonder-inference` on a loopback base URL needs no consent. Any other
  host is refused before a byte is sent unless `SONDER_ALLOW_REMOTE_INFERENCE=1`,
  the URL is `https://`, `SONDER_INFERENCE_API_KEY` is set (it is redacted
  from logs), and the operation context allows cloud. The optional
  `SONDER_INFERENCE_FALLBACK=ollama` reaches only local Ollama: the fallback
  call runs with cloud and remote-Ollama consent withdrawn, so a tier mapped
  to a hosted model is refused, and it cannot widen where a prompt goes.
  Inference traffic ignores proxy settings and never follows redirects
  ([provider reference](../architecture/sonder-inference-provider.md)).
- **`SONDER_ALLOW_PRIVATE_COT` takes a second, separate act.** It is the one
  consent gate an environment variable cannot open by itself:
  `admin_private_chain_of_thought` also requires an explicit `allow` rule for
  its own name in `permissions.json`. Write it with the developer-gated
  `permission_rule_set`, or by hand — the act is that state on disk, not one
  route to it, so filesystem access to the Sonder home is enough to set it and
  that file belongs inside the trust boundary. The built-in rule denies it and
  the tool reads that rule itself, so a variable inherited from a parent
  process is not enough.
  Opted in it serves the same record as `reasoning_show` — the model's own
  thinking channel for the current turn — and nothing besides it. That channel
  can hold what the final answer deliberately left out.
- **Unattended callers get a fourth route out: approve exactly one call.**
  With nobody at the console, file changes, host programs and destructive
  tools are refused (`permission_modes`, `UNATTENDED_REFUSED_RISKS`) and the
  refusal names its remedies. A refusal of a call whose arguments reached the
  gate also carries a *call id* — the digest of the tool name and its
  credential-free arguments — and the call is noted as pending in
  `approvals.db` (`SONDER_APPROVALS_DB`). `/approvals` lists those;
  `/approve <call id>` at the console (or `permission_approve` with a
  developer token, or with `SONDER_ALLOW_PERMISSION_EDITS=1`) approves that
  one call once, and the next unchanged call from any surface spends it. An
  approval is one tool and one digest, spent atomically, and expires (60 s to
  24 h, 15 min by default); it never widens a rule and never overrides
  `plan`, an explicit `deny`, or the durable-authority class —
  `permission_approve` is itself in that class, so no unattended caller can
  approve its own next call. The ledger stores digests and a bounded,
  redacted preview, never arguments. A spent approval also carries exactly
  the reach the operator approved: the call's own `extra_roots` are honoured
  for that call alone, with containment still checked against them. The
  shared `SONDER_FILE_APPROVAL_CODE` is retired (it warns once when set); a
  model's string `token` or `approval` is dropped from every agent proposal. The isolated-run secrets
  (`SONDER_ISOLATED_APPROVAL_CODE`, `SONDER_ISOLATED_WRITE_APPROVAL_CODE`) are
  retired the same way; a writable isolated workspace needs a one-shot approval
  of exactly that call.
- **Only an attended caller can raise the permission mode.** The
  `permission_mode` tool stays exempt from the gate so a client in `plan` can
  always get back to `manual`, but an unattended caller (an MCP client, the
  HTTP chat's `/permission_mode`, a control command) asking for a mode above
  both the current one and `manual` -- `acceptEdits`/`auto` from `manual`,
  `auto` from `acceptEdits` -- is refused with the remedy named: `/mode <m>`
  at the console, or an administrator's `POST /v1/permission-mode`. Lowering
  the mode is allowed from every surface. The mode persists in `SONDER_HOME`
  and governs every surface sharing it, which is why raising it is attended.
- **A worker whose lease is gone may look but not touch.** The autopilot
  controller installs an effect fence for each task
  (`sonder_runtime/adapters/execution/effect_fence.py`); the permission gate
  consults it before every effect-class tool on that thread and refuses
  (`source="fence"`, with a receipt) once the run's lease is lost or the run
  is cancelled, whatever the mode or rules say. Reads are never fenced.

## Authentication

- Bearer `SONDER_API_KEY`, compared in **constant time**.
- Rotation with an overlap window: the previous key's **hash** (never the
  plaintext) is stored with a mandatory expiry; both keys work until it
  lapses (`rotate-key`, [rotate-credentials](../runbooks/rotate-credentials.md)).
- A per-peer **auth-failure token bucket** throttles credential guessing;
  failures emit `AUTH_FAILED` audit events and a bounded metric.
- Privileged routes (drain, update control) require an **admin**
  authorization result, not merely a valid chat key.
- **Console logins never print the session token.** `/login` in the REPL (and
  in `repl --json`) and in the served console keeps the bearer token for that
  session and shows `token: [hidden] ...` in its place. The `admin_login` MCP
  tool and `POST /v1/sonder/login` still return the token, because returning
  it is their contract with a programmatic client.

## Workspace & tool containment

- **Guarded file tools** operate only inside configured roots
  (`SONDER_FILE_ROOTS`); path canonicalization blocks traversal and
  symlink escape. Deletes are dry-run unless an explicit confirm matches.
- **Credential stores are denied by default, even inside a root.** The direct
  read tools (`file_read`, `file_read_range`, `data_inspect`, `image_inspect`,
  and the source of `file_copy`/`file_move`) refuse `.ssh`, `.aws`, `.azure`,
  `.gnupg`, `.kube`, `.docker/config.json`, `.git/config`, `.netrc`,
  `.git-credentials`, `.pgpass`, OpenSSH key files (`id_rsa`, `id_ed25519`,
  ...) and `.env*` -- regardless of a developer token or `SONDER_FILE_BYPASS`.
  The only way to read one is a root that names it: add the store directory
  itself (`~/.ssh`) or the exact file (`/proj/.env`) to `file_roots.local` or
  `SONDER_FILE_ROOTS` (a one-shot `/approve` of a call whose `extra_roots`
  names it works once). Classified secrets inside a named store (key files,
  `.env`) still need a developer token as before.
- **Permission policy** (`domain/execution/policy.py`, `permission_rules.py`)
  is first-match glob: `allow` / `ask` / `deny`, defaulting to `ask`, with
  `file_delete` denied and read-only status tools allowed by default.
- **Process execution** is argv-only with bounded timeout and output; the
  code runner is confined to the workspace cwd. It is a containment layer,
  not a sandbox — it does not replace OS isolation.

- **Local HTTP probes** require an explicit port and pin direct connections to
  DNS answers that are exclusively loopback. DNS is checked again before
  connect; proxies, credentials, cookies, authorization, fragments, sensitive
  control-state paths, and non-loopback redirects are refused.

## Data protection

- **Redaction before logging** (`sonder_logging.py`): bearer tokens, API
  keys, known secret env values, URL credentials, private-key blocks,
  free-standing known provider credentials (AWS `AKIA`/`ASIA` keys, GitHub,
  GitLab, OpenAI, Hugging Face, npm, PyPI, Google, Stripe, Slack tokens and
  JWTs), and configured workspace path prefixes are stripped. The provider
  formats live once in `domain/security/credential_formats.py` and are shared
  by the log redactor, the domain redaction set and the contribution privacy
  classifier. A redaction failure
  replaces the whole detail with `[REDACTION_FAILED]` and increments a
  metric — it degrades observability, never privacy.
- **operations.db** stores identifiers, counts, hashes, durations, and
  redacted paths only — never prompts, memory text, workspace contents, or
  credentials.
- **Session capture is redacted before it is stored**: prompts, messages,
  tool arguments/results and provider payloads written to `sessions.db` pass
  through the same redactor first, and the stored (redacted) form is what
  replay and export read, so replay stays deterministic. `sessions.db` is
  still a private store: do not share it any more than `memory.db`.
- **Recall is project-scoped**; cross-project recall requires an explicit
  override.
- **Owner-only state on POSIX** (`sonder_runtime/platform/private_files.py`):
  the state home (`SONDER_HOME`) is created `0700`, and every SQLite store
  opened through the connection factory (`memory.db`, `sessions.db`,
  `approvals.db`, `goals.db`, ...) plus its `-wal`/`-shm`/`-journal` sidecars
  and the tool-audit JSONL are created `0600`. An existing home or store left
  group/world-readable by an older build is tightened when it is next opened,
  but only if the current user owns it; symlinks, other accounts' files, sticky
  shared directories (`/tmp`), `/` and the user's own home directory are never
  changed, and no mode is ever widened. Windows is unchanged: the default home
  under `%LOCALAPPDATA%` inherits a user-only ACL, and a custom `SONDER_HOME`
  there needs an equivalent ACL set by the operator. On a host that runs
  uid-separated self-modification candidates (`SONDER_SELFMOD_CANDIDATE_UID`
  set) the home is `0711` instead -- the candidate uid can traverse it to its
  workspace under `selfmod/workspaces` but cannot list or read it, and the
  stores stay `0600`. A home already tightened to `0700` before that uid was
  configured is never widened automatically: run `chmod 0711 "$SONDER_HOME"`
  once when enabling Linux candidate isolation. A home whose group/other bits
  are already traverse-only is left alone by every process (the served
  runtime usually runs without that variable), so that step is not undone.

## Update trust

Signed engine distribution uses The Update Framework: releases are
accepted only through a signed metadata chain with threshold keys, hash
verification, rollback/freeze protection, and adversarially-safe archive
extraction. See [Update Manager](13-update-manager.md).

## Incident procedures

Runbooks: [suspected-secret-exposure](../runbooks/suspected-secret-exposure.md),
[rotate-credentials](../runbooks/rotate-credentials.md),
[database-lock-or-corruption](../runbooks/database-lock-or-corruption.md),
[ollama-outage](../runbooks/ollama-outage.md).

## Threat-model boundaries (honest scope)

Sonder protects a **single owner's** private runtime. It is **not**
multi-user authz (the identity seam exists, defaulting to one owner, but
there is no multi-tenant enforcement), not a code sandbox, and not a
defense against a compromised host or a malicious Ollama binary. The
guardrails raise the bar for an over-eager model and an exposed endpoint;
they are not a substitute for OS-level isolation.
