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
  (`SONDER_ALLOW_REMOTE_OLLAMA`), approximate location, model reasoning
  (`SONDER_EXPOSE_REASONING`), and private chain-of-thought
  (`SONDER_ALLOW_PRIVATE_COT`). Runtime policy can never turn any of these on.
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

## Authentication

- Bearer `SONDER_API_KEY`, compared in **constant time**.
- Rotation with an overlap window: the previous key's **hash** (never the
  plaintext) is stored with a mandatory expiry; both keys work until it
  lapses (`rotate-key`, [rotate-credentials](../runbooks/rotate-credentials.md)).
- A per-peer **auth-failure token bucket** throttles credential guessing;
  failures emit `AUTH_FAILED` audit events and a bounded metric.
- Privileged routes (drain, update control) require an **admin**
  authorization result, not merely a valid chat key.

## Workspace & tool containment

- **Guarded file tools** operate only inside configured roots
  (`SONDER_FILE_ROOTS`); path canonicalization blocks traversal and
  symlink escape. Deletes are dry-run unless an explicit confirm matches.
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
  keys, known secret env values, URL credentials, private-key blocks, and
  configured workspace path prefixes are stripped. A redaction failure
  replaces the whole detail with `[REDACTION_FAILED]` and increments a
  metric — it degrades observability, never privacy.
- **operations.db** stores identifiers, counts, hashes, durations, and
  redacted paths only — never prompts, memory text, workspace contents, or
  credentials.
- **Recall is project-scoped**; cross-project recall requires an explicit
  override.

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
