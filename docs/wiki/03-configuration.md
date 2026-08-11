# Configuration

Two layers, deliberately separate:

1. **Production configuration** (`sonder_config.py`) — typed, validated,
   **fail-closed**, read once at startup. Governs bind, auth, TLS, limits,
   state paths, features, capacity, observability, backups.
2. **Runtime policy** (`runtime_policy.py`) — hot-reloadable JSON that
   selects model aliases, routing lanes, and NPU modes. It can **never**
   widen network, filesystem, credential, or cloud permissions.

## Precedence (production config)

Lowest to highest:

```
built-in safe defaults
  < profile TOML (/etc/sonder/sonder.toml)
  < secrets env file (/etc/sonder/sonder.env, mode 0600)
  < process environment (SONDER_* / OLLAMA_*)
  < explicit --set command-line overrides
```

Validation collects **all** errors and reports them together; no listener
opens until every mandatory check passes. Secrets are **never** accepted
in TOML — a secret key there is a validation error.

```bash
python -m sonder_runtime config --config /etc/sonder/sonder.toml \
    --secrets /etc/sonder/sonder.env      # prints the effective, redacted config
```

## `sonder.toml` (see `packaging/sonder.toml.example`)

```toml
schema_version = 1
profile = "server-private"          # or "workstation-local"

[server]
host = "127.0.0.1"                   # non-loopback needs the two rules below
port = 11435
auth_mode = "api-key"               # api-key | account | both | either
max_request_bytes = 1048576
max_concurrent_requests = 4
request_timeout_seconds = 300
tls_terminated_by_proxy = false     # true required for a non-loopback bind
trusted_proxy_cidrs = ["127.0.0.1/32", "::1/128"]

[state]
home = "/var/lib/sonder"
workspace_roots = ["/srv/sonder/workspaces"]
minimum_free_disk_bytes = 5368709120

[ollama]
url = "http://127.0.0.1:11434"
allow_remote = false                # remote-Ollama consent gate

[features]
cloud = false                       # hosted-model consent gate
web = false                         # web tools consent gate
training = false
npu = false

[capacity]
http_requests = 4
queue_depth = 32
# ... fleet_workers, autopilot_runs, training_jobs, etc.

[observability]
log_format = "json"
metrics_enabled = true
audit_retention_days = 90

[backup]
enabled = true
target = "/var/backups/sonder"
retention_daily = 7
retention_weekly = 4
retention_monthly = 6
```

### The bind-security rule

A non-loopback `[server].host` is **rejected before bind** unless *both*
`tls_terminated_by_proxy = true` and a `SONDER_API_KEY` of ≥24 chars are
set. The reference topology keeps Sonder on loopback behind a TLS proxy
regardless. See [Security Model](09-security-model.md).

## Secrets file (`packaging/sonder.env.example`, mode 0600)

```
SONDER_API_KEY=...            # bearer key clients present
SONDER_AUTH_SECRET=...        # HMAC secret for account tokens
```

The loader refuses a group/world-readable secrets file on POSIX. Rotate
with `python -m sonder_runtime rotate-key` (overlap window; old key valid
until expiry). See [rotate-credentials](../runbooks/rotate-credentials.md).

## Environment variables (compatibility surface)

Core paths/identity: `SONDER_HOME`, `SONDER_DB`, `SONDER_AUTOPILOT_DB`,
`SONDER_FLEET_DB`, `SONDER_OPERATIONS_DB`, `SONDER_UPDATES_DB`,
`SONDER_RUNTIME_POLICY`, `SONDER_TRAINING_STATE`.

Serving/auth: `SONDER_API_KEY`, `SONDER_HOST`, `SONDER_PORT`,
`SONDER_AUTH_MODE`, `SONDER_AUTH_SECRET`, `SONDER_MAX_REQUEST_BYTES`,
`SONDER_MAX_CONCURRENT_REQUESTS`, `SONDER_QUEUE_DEPTH`, `SONDER_CORS_ORIGINS`.

Orchestration: `SONDER_MAX_WORKER_CAP` lowers the absolute ceiling for explicit
per-run `worker_cap` requests. It accepts a positive decimal integer only and
is clamped to the compiled ceiling of 64. Invalid, boolean, fractional,
non-finite, zero, or negative values fail safe to the historical 16-worker
ceiling and are visible in `master_capacity`. Without a per-run `worker_cap`,
the conservative hardware-derived worker width is unchanged.

Consent gates: `SONDER_ALLOW_CLOUD`, `SONDER_WEB_TOOLS`,
`SONDER_ALLOW_REMOTE_OLLAMA`, `SONDER_FILE_ROOTS`, `SONDER_LOCATION_CONSENT`,
`SONDER_EXPOSE_REASONING`, `SONDER_ALLOW_PRIVATE_COT`.

`SONDER_EXPOSE_REASONING` decides whether Sonder asks a reasoning model for its
thinking at all; with it off nothing is captured, so nothing can be shown. It
also means the model is asked to think, which costs latency and tokens.
`SONDER_ALLOW_PRIVATE_COT` is separate and narrower: it decides whether
`admin_private_chain_of_thought` (`/cot`) may reveal that record, and it is
**not sufficient on its own** — the tool also requires an explicit `allow` rule
for its own name in `permissions.json`, since the built-in rule denies it.
Write that rule with `permission_rule_set` or by hand; what the gate requires
is the state on disk, not any one route to it. Both acts are required; either
alone still refuses.

Models/tiers: `SONDER_FAST`, `SONDER_CODE`, `SONDER_GENERAL`,
`SONDER_REASONING`, `SONDER_VISION`, `SONDER_BASE_MODEL`,
`SONDER_EMBED_MODEL`, `SONDER_CONTEXT_SIZE`, `SONDER_LEARN_TIERS`.
`SONDER_REASONING` / `SONDER_VISION` also accept `none` (or `off`) to leave
that specialist tier unbound, in which case reasoning/vision work falls back
to a base tier.

Behavior toggles: `SONDER_SPECULATION` (0 disables speculative execution),
`SONDER_METRICS`, `OLLAMA_HOST`.

Update/publish (optional): `SONDER_RELEASES_DIR`, `SONDER_CURRENT_LINK`,
`SONDER_UPDATE_ALLOW_UNSIGNED` (dev only), `SONDER_BRANCH_PREDICTOR`.

`python -m sonder_runtime diagnostics` prints a redacted bundle of the
effective configuration, schema state, and preflight for support.
