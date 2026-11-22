# Security Model

Sonder is private-first. Its guarantees are **host-enforced**, independent
of the model — an uncensored or "abliterated" model changes what it will
*discuss*, never what the runtime will *let it do*.

## Network posture

- **Loopback by default.** The HTTP server binds `127.0.0.1`. A
  non-loopback bind is **rejected before the socket opens** unless both
  `tls_terminated_by_proxy = true` and strong credentials for the auth mode are
  set: `api-key` needs a `SONDER_API_KEY` of at least 24 characters, `account`
  an account secret of at least 32, and `both` and `either` need both;
  `local-open` never qualifies. The reference deployment keeps loopback behind
  a TLS reverse proxy regardless
  ([secure-remote-access](../runbooks/secure-remote-access.md)). Setup-time
  checklist: [security-hardening](../runbooks/security-hardening.md).
- **Consent gates**, each independent and default-off: cloud/hosted models
  (`SONDER_ALLOW_CLOUD`), web tools (`SONDER_WEB_TOOLS`), remote Ollama
  (`SONDER_ALLOW_REMOTE_OLLAMA`), private-node whole-job compute
  (`SONDER_ALLOW_REMOTE_COMPUTE`, additionally requiring per-workload
  `allow_remote`), approximate location, model reasoning
  (`SONDER_EXPOSE_REASONING`), and private chain-of-thought
  (`SONDER_ALLOW_PRIVATE_COT`). Runtime policy can never turn any of these on.
- **Remote Sonder Inference needs four things at once:**
  `SONDER_ALLOW_REMOTE_INFERENCE=1`, an `https://` URL, a
  `SONDER_INFERENCE_API_KEY` and an operation context that allows cloud; a
  loopback base URL needs none of them. The fallback to local Ollama cannot
  widen where a prompt goes. Rules and failure modes:
  [provider reference](../architecture/sonder-inference-provider.md),
  setup: [sonder-inference](../runbooks/sonder-inference.md).
- **Remote Ollama is its own gate:** `SONDER_ALLOW_REMOTE_OLLAMA=1` and an
  `https://` origin with an explicit port and no path
  ([remote-ollama-caddy](../runbooks/remote-ollama-caddy.md#coordinator-settings)).
- **Private Sonder Inference workers are their own consent lane.**
  `SONDER_INFERENCE_PRIVATE_WORKERS` names operator-approved private servers
  (https, private IP literal, a dedicated CA bundle and token variable). It
  works only with `SONDER_ALLOW_REMOTE_INFERENCE=1`, lets prompts reach those
  servers without cloud consent, and neither grants nor depends on cloud,
  remote-Ollama or remote-compute consent
  ([private workers](../architecture/sonder-inference-provider.md#private-workers)).
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

Setup (choosing a mode, creating the key or accounts) is in
[configure-authentication](../runbooks/configure-authentication.md); rotation
is in [rotate-credentials](../runbooks/rotate-credentials.md). The model:

- Bearer `SONDER_API_KEY`, compared in **constant time**. With no key and no
  account requirement the listener is `local-open`: every loopback caller is
  an administrator and developer, so it is only for a single-user machine.
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

- **Guarded file tools** operate only inside the allowed roots: Sonder's own
  checkout, the state home, `SONDER_FILE_ROOTS` (which replaces
  `[state].workspace_roots` when both are set) and the lines of
  `<home>/file_roots.local` (or the file named by `SONDER_FILE_ROOTS_FILE`).
  `SONDER_FILE_BYPASS` (process-wide) or a developer token turns root
  containment off for every path, with no `extra_roots` needed (inside
  managed console work containment is kept and bypass only adds
  `extra_roots`); credential
  stores are still refused. A one-shot approval of a call is the only way to
  widen the roots for that call with containment still checked. Path
  canonicalization blocks traversal and
  symlink escape. Deletes are dry-run unless an explicit confirm matches.
  Setup: [security-hardening](../runbooks/security-hardening.md#file-roots).
- **Managed console work** is granted `[state].workspace_roots` plus, by a
  deliberate policy addition, exactly one more tree: the app-owned default
  workspace root (`%USERPROFILE%\Sonder\workspaces`, `~/Sonder/workspaces`,
  or `[state].default_workspace_root`), where the console creates folders
  for work started without one. Sonder creates that root itself, and grants
  it only while it exists, is not a link or junction, is outside every Sonder
  source checkout and overlaps no private control state; otherwise it is left
  out. The overlap rule for configured roots is unchanged: a configured root
  that overlaps private control state still refuses every managed request
  (`bootstrap/repl_managed.py`). The grant does not add the root to the
  global file roots above.
- **Credential stores are denied by default, even inside a root.** The direct
  read tools (`file_read`, `file_read_range`, `data_inspect`, `image_inspect`,
  and the source of `file_copy`/`file_move`) refuse `.ssh`, `.aws`, `.azure`,
  `.gcloud`, `.config/gcloud`, `AppData/Roaming/gcloud`, `.gnupg`, `.kube`,
  `.docker/config.json`, `.git/config`, `.config/gh/hosts.yml`,
  `AppData/Roaming/GitHub CLI/hosts.yml`, `.config/glab-cli/config.yml`,
  `.cargo/credentials[.toml]`, `.gem/credentials`, `.m2/settings.xml` and
  `settings-security.xml`, `.netrc`, `.git-credentials`, `.pgpass`, `.npmrc`,
  `.pypirc`, `kubeconfig`, `.terraformrc`/`terraform.rc`,
  `credentials.tfrc.json`, `.boto`, `.s3cfg`, `.htpasswd`, `.vault-token`,
  `*.kdbx`, GCP key files (`service-account.json`,
  `application_default_credentials.json`), OpenSSH key files (`id_rsa`,
  `id_ed25519`, ...) and `.env*` -- regardless of a developer token or
  `SONDER_FILE_BYPASS`. Matching is case-insensitive and uses the root's
  trailing directories too, so a root set at `~/.config` or `~/.config/gh`
  does not unlock `gcloud/...`, `hosts.yml` or (for `~/.docker`)
  `config.json`; only a root that is the store itself (`~/.ssh`,
  `~/.config/gcloud`) or the exact file does.
  `archive_create` refuses the same stores by full path, so a store cannot be
  archived, extracted elsewhere and read. `archive_extract` deliberately does
  not refuse a whole archive for a name that only the token-store additions
  flag (`.npmrc`, `kubeconfig`, `.htpasswd`, `plain.kdbx`, ...): ordinary
  repositories ship them. They are written, and reading the extracted file is
  refused. Extraction refuses on the pre-existing name rules
  (`_is_base_secret_path`) exactly as before, so `.env*` (including
  `.env.prod.kdbx`), key files, `*credential*`/`*secret*` config files (this
  includes `application_default_credentials.json` and
  `credentials.tfrc.json`), `.ssh`, `.aws` and so on are still refused.
  The only way to read one is a root that names it: add the store directory
  itself (`~/.ssh`) or the exact file (`/proj/.env`) to `file_roots.local` or
  `SONDER_FILE_ROOTS` (a one-shot `/approve` of a call whose `extra_roots`
  names it works once). Classified secrets inside a named store (key files,
  `.env`) still need a developer token as before.
- **Permission policy** (`domain/execution/policy.py`, `permission_rules.py`)
  is first-match glob: `allow` / `ask` / `deny`, defaulting to `ask`, with
  `file_delete` denied and read-only status tools allowed by default. Modes,
  rules and approvals are set up in
  [security-hardening](../runbooks/security-hardening.md).
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
  classifier. Assignment labels include password shorthand `pwd` and
  `aws_secret_access_key` / `secret_access_key` (case-insensitive; hyphens or
  underscores). Default session exports use the canonical domain policy;
  key-aware structure walks recognize these labels as well. Plain label
  mentions and unrelated identifiers remain unchanged. A redaction failure
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
  changed, and no mode is ever widened. On Windows, where modes do not
  exist, Sonder instead makes a best-effort attempt to give the state home and
  its secret stores a protected ACL granting only the current user, SYSTEM and
  Administrators, because the profile's inherited ACL cannot be assumed to be
  user-only. It never touches paths owned by another account, and failures are
  only logged at debug level. A secrets file kept outside the state home needs
  an ACL set by the operator
  ([security-hardening](../runbooks/security-hardening.md#secrets-file)). On a host that runs
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
