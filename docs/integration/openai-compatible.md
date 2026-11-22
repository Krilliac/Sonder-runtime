# OpenAI-compatible provider

Route one or more Sonder tiers to any server that speaks the OpenAI HTTP API:
a local llama.cpp `llama-server`, vLLM, LM Studio, a reverse proxy in front of
one of those, or a hosted service. Verified against the code on 2026-11-20.

Unlike [OpenRouter](openrouter.md) and [Anthropic](anthropic.md), this provider
has **one fixed endpoint and no model catalog**: one base URL (and optionally
one model name) serves every tier bound to it. A loopback endpoint (a server
on the same machine) is local and needs no consent. Any other host is treated
as hosted: prompts leave the machine, so it needs cloud consent and HTTPS.

Implementation: `sonder_runtime/adapters/inference/openai_compat_gateway.py`
(gateway), `sonder_runtime/adapters/provider_bindings.py` (names and
bindings), `scripts/backend_attest.py` (diagnostic probe).

## Prerequisites

- Sonder installed and running ([setup-start-here](../runbooks/setup-start-here.md)).
- An OpenAI-compatible server you can reach, started and serving a model. Its
  own install and model download are outside this repo's docs; see that
  project's documentation.
- Know the server **root**, for example `http://127.0.0.1:8080`, not the API
  path.

## 1. Bind a tier (or all tiers) to the provider

Provider selection is read from environment variables
(`sonder_runtime/adapters/provider_bindings.py:171-198`). Accepted names for
this provider: `openai`, `openai-compatible`, `openai_compatible`, `llamacpp`,
`vllm` (`provider_bindings.py:20-24`); they are all the same provider.

```powershell
# Every tier:
$env:SONDER_MODEL_BACKEND = 'openai-compatible'
# Or only some tiers (the others stay on SONDER_MODEL_BACKEND, default ollama):
$env:SONDER_CODE_PROVIDER = 'openai-compatible'
$env:SONDER_REASONING_PROVIDER = 'openai-compatible'
```

```bash
export SONDER_MODEL_BACKEND=openai-compatible
export SONDER_CODE_PROVIDER=openai-compatible
```

Per-tier variables exist for `fast`, `general`, `code`, `reasoning` and
`vision` only (`provider_bindings.py:9-16`). The `cloud-*` tiers are not
bindable. An unknown name is refused at startup with an error, never silently
sent to Ollama.

Memory-recall embeddings do **not** move with this setting: they use the
Ollama-only embedder (`adapters/recall_gateway.py`, `adapters/embeddings.py`),
so keep Ollama available for them. `SONDER_EMBEDDING_PROVIDER` (default: the
value of `SONDER_MODEL_BACKEND`, `provider_bindings.py:184-186`) selects the
provider for gateway-level embeddings only
(`adapters/provider_dispatch/gateway.py:106`). Binding it to
`openai-compatible` sends those to `/v1/embeddings` on your endpoint (see
step 3).

Where these variables must live, and why a restart is required, is in
[Where provider variables must live](../wiki/08-model-tiers-and-gateway.md#where-provider-variables-must-live).
Short version: set them in the process environment (or, under the packaged
systemd unit, in `/etc/sonder/sonder.env`), then restart Sonder.

## 2. Point at the server

```powershell
$env:SONDER_OPENAI_BASE_URL = 'http://127.0.0.1:8080'   # server ROOT, no /v1
$env:SONDER_OPENAI_MODEL = 'my-model'                   # name the server serves
```

The key is deliberately not shown as a typed assignment: a literal
`$env:SONDER_OPENAI_API_KEY = '<key>'` is saved in PSReadLine history. Prompt for
it instead, or store it as a user environment variable:

```powershell
$k = Read-Host 'API key' -AsSecureString
$env:SONDER_OPENAI_API_KEY = [System.Net.NetworkCredential]::new('', $k).Password
```

- `SONDER_OPENAI_BASE_URL` is **required**; without it the first call fails
  with "no OpenAI-compatible endpoint configured (set SONDER_OPENAI_BASE_URL)"
  (`openai_compat_gateway.py:270`, `:350-355`).
- **Do not end the URL with `/v1`.** The gateway appends `/v1/chat/completions`
  and `/v1/embeddings` itself (`:493`, `:545`, `:716`). A base URL of
  `http://127.0.0.1:8080/v1` therefore requests `/v1/v1/chat/completions`,
  which servers answer with 404, and Sonder reports "endpoint rejected request
  (HTTP 404)" (`:752`).
- `SONDER_OPENAI_API_KEY`, when set, is sent as `Authorization: Bearer <key>`
  on every request, loopback included (`:271`, `:622-623`). Keep it out of
  committed files and command lines.
- `SONDER_OPENAI_MODEL` selects the model name sent in each request. Order:
  an explicit `model` request option, then `SONDER_OPENAI_MODEL`, then the
  **tier name** (for example `code`), then `default` (`:466`). Most servers
  reject a tier name as a model, so set it. With it unset, Sonder also refuses
  tool-using lanes on that tier; the explanation and fix are in
  [Configuration](../wiki/03-configuration.md) (search for
  `SONDER_OPENAI_MODEL`).
- `SONDER_OPENAI_EMBED_MODEL` is the embedding model name for `/v1/embeddings`;
  default `text-embedding-3-small` (`:273`, `:542`).
- The request timeout is fixed: 300 seconds when the operation has no tighter
  deadline (`_DEFAULT_TIMEOUT`, `:110`, `:902`); there is no variable to change
  it. An operation deadline can only shorten it.

## 3. Embeddings through the endpoint (optional)

Only if your server serves `/v1/embeddings` and you want gateway embeddings
there:

```powershell
$env:SONDER_EMBEDDING_PROVIDER = 'openai-compatible'
$env:SONDER_OPENAI_EMBED_MODEL = 'my-embedding-model'
```

Leave it unset to keep embeddings on the same provider as
`SONDER_MODEL_BACKEND`. If you bind everything to `openai-compatible` and your
server has no embeddings route, set `SONDER_EMBEDDING_PROVIDER=ollama`.

## 4. Verify

1. Preview the probe (no provider call, no evidence file written):

   ```powershell
   python scripts/backend_attest.py --base-url http://127.0.0.1:8080 --model my-model --dry-run
   ```

   Use literal values, or set both `SONDER_OPENAI_BASE_URL` and
   `SONDER_OPENAI_MODEL` first: an unset `$env:...` is `$null`, and PowerShell
   (every version) drops a `$null` argument to a native command. The script
   then exits 2 with `argument --model: expected one argument` (or the same
   for `--base-url`). Expect one JSON line containing `"dry_run": true`,
   exit code 0. Startup can still create the private state directory
   (the `--evidence` default calls `state_path`, `scripts/backend_attest.py:168`,
   `platform/paths.py:239-248`); set `SONDER_HOME` to a scratch directory to
   avoid that.
2. Run it for real (drop `--dry-run`). Expect JSON with `passed`, `failed`,
   `unknown` lists; exit 0 when nothing failed, 1 otherwise. Details and
   limits of what this proves: [BACKEND-ATTESTATION](../BACKEND-ATTESTATION.md).
3. Restart Sonder and send a chat turn on a bound tier.

## Transport rules

All enforced before any request is sent (`openai_compat_gateway.py:372-440`):

| Rule | Detail |
|---|---|
| Loopback set | `127.0.0.1`, `localhost`, `::1`, `0.0.0.0` (`:109`). Loopback needs no cloud consent and may use plain `http`. |
| Non-loopback needs HTTPS | Otherwise the call fails with an error that names `SONDER_OPENAI_ALLOW_HTTP_NETWORKS`. |
| Plain `http` on a private network | Set `SONDER_OPENAI_ALLOW_HTTP_NETWORKS` to one or more CIDRs, comma or semicolon separated, for example `192.0.2.0/24`. It applies only when the URL host is a **private IP literal** inside a listed private CIDR; host names never qualify, because DNS, not you, picks the peer (`:117`, `:274`, `:423-440`). A bad CIDR entry is an error only when the URL host is a private IP literal; for a host name the check returns before any entry is parsed (`:420-436`). |
| No credentials in the URL | `user:password@host` and an `@` before the query are refused; use `SONDER_OPENAI_API_KEY`. |
| No redirects | A 3xx is an error, never followed, so the key is never forwarded to another origin (`:132-150`, `:898-902`). |
| Proxies | Loopback requests bypass every proxy setting (`http_proxy` etc.); remote ones keep proxy support. |
| No `%` in the host | Percent-encoding in the host is refused. |

## Cloud consent

A non-loopback endpoint requires a cloud-allowing operation context
(`:365-370`). The single list of ways to grant consent, and where it can never
be granted, is in
[Remote & cloud](../wiki/08-model-tiers-and-gateway.md#remote--cloud-opt-in).
For a quick start: `SONDER_ALLOW_CLOUD=1` in the Sonder process. This is
**process-wide**: it also enables the Ollama `cloud-*` tiers, OpenRouter and
Anthropic wherever those are configured. The consent map is in the wiki page
above.

Without it a hosted endpoint fails with error code `FORBIDDEN` and the text
"endpoint '<url>' is non-loopback but this operation context does not allow
cloud". The A2A surface never grants consent
(`interfaces/http/facades/a2a_jsonrpc.py:174`), so bound tiers on a
non-loopback endpoint cannot be reached from A2A.

## Streaming

Streaming requests send `stream_options: {"include_usage": true}`
(`:478`). A server that rejects that field answers HTTP 400 and Sonder reports
"endpoint rejected request (HTTP 400)"; there is no switch to omit it, so use a
server version that accepts it.

Two optional stall limits apply to streaming reads
(`:117-119`, `:250-263`); both default to 0, which **disables** the limit.
Their exact semantics (what counts as idle, the single-JSON-body case) have one
home: the `SONDER_INFERENCE_STREAM_*` rows in
[Sonder Inference provider](../architecture/sonder-inference-provider.md); the
OpenAI-compatible variables have the same meaning.

| Variable | Meaning |
|---|---|
| `SONDER_OPENAI_STREAM_IDLE_TIMEOUT_SECONDS` | Seconds with no data mid-stream before the call fails. |
| `SONDER_OPENAI_STREAM_FIRST_CONTENT_TIMEOUT_SECONDS` | Seconds to wait for the first content before the call fails. |

Each must be a number in `[0, 86400]`, else Sonder refuses to start the call
with a message naming the variable. A tripped limit surfaces as a deadline
error.

## Examples

Default ports below are the servers' own documented defaults, taken from
those projects, not from this repo. **Unverified here**: confirm with the
server's help output. Each is expected to serve `/v1/chat/completions`.

| Server | Typical base URL | Model name |
|---|---|---|
| llama.cpp `llama-server` | `http://127.0.0.1:8080` | whatever `--alias`/the loaded model reports; many builds accept any name |
| vLLM (`vllm serve`) | `http://127.0.0.1:8000` | the model id passed to `vllm serve` |
| LM Studio local server | `http://127.0.0.1:1234` | the identifier shown in LM Studio |

```powershell
# llama.cpp example
$env:SONDER_CODE_PROVIDER = 'llamacpp'
$env:SONDER_OPENAI_BASE_URL = 'http://127.0.0.1:8080'
$env:SONDER_OPENAI_MODEL = 'my-model'
```

If a loopback server is bound only to the loopback interface and Sonder runs
on another machine, do not expose it in plain text: use the proxy below.

## Behind a TLS reverse proxy

To reach a server on another machine, put an HTTPS proxy (for example Caddy)
in front of it and use the HTTPS name. Caddy install, a hardened Caddyfile with
a source-IP allowlist, firewall rules and verification are in
[remote-ollama-caddy](../runbooks/remote-ollama-caddy.md) (written for Ollama;
the proxy and firewall parts apply unchanged, with your server's port as the
upstream). The minimal shape is:

```text
# Caddyfile on the server host (Caddy syntax is external to this repo).
# NOTE: this snippet does no authentication. 10.0.0.1/32 is the Sonder
# machine's address as this host sees it; replace it.
llm.example.com {
    route {
        @not_coordinator {
            not remote_ip 10.0.0.1/32
        }
        respond @not_coordinator "forbidden" 403

        reverse_proxy 127.0.0.1:8080
    }
}
```

**Security: the `remote_ip` matcher is the only thing in this snippet that
restricts access, and there is no authentication.** Without it the model server
would be published to anyone who can reach port 443. Also do both of these:

1. Make something enforce the key. Either start the upstream with its own key
   check (for example the server's API-key option), or add authentication in
   Caddy. Sonder only *sends* `SONDER_OPENAI_API_KEY`; nothing in this snippet
   or in Sonder checks it.
2. Keep the `remote_ip` matcher shown above (403 for every other source) and
   add a host firewall rule for the same address. The firewall commands and
   the full hardened Caddyfile are in the runbook above.

A public name such as `llm.example.com` makes Caddy obtain an ACME certificate,
which fails once 443 is limited to the coordinator and port 80 is closed; see
"Public ACME certificates" in the
[runbook](../runbooks/remote-ollama-caddy.md), or use `tls internal`.

```powershell
$env:SONDER_OPENAI_BASE_URL = 'https://llm.example.com'   # root, no /v1
$env:SONDER_ALLOW_CLOUD = '1'                              # non-loopback consent
# The key (the one the upstream or proxy enforces): use the Read-Host
# -AsSecureString pattern from section 2, not a typed value.
```

The `Authorization: Bearer` header must reach the upstream (reverse proxies
normally pass it through; if you authenticate at the proxy instead, configure
it there). Do not put credentials in the URL, and do not rely on redirects.
Remember the endpoint is non-loopback, so every prompt on the bound tiers
leaves the machine: only do this for hosts you control.

**Certificate trust.** The gateway verifies the certificate and hostname with
Python's default verifying context, which uses the operating-system trust
store and honours `SSL_CERT_FILE` / `SSL_CERT_DIR`
(`openai_compat_gateway.py:128-135`, `adapters/tls_contexts.py:56-57`, `:83`). A
public CA certificate works as is. A private name, or Caddy `tls internal`,
fails TLS until the issuing CA is trusted by the Sonder process: add it to the
operating-system trust store (preferred), or point `SSL_CERT_FILE` at a full CA bundle that includes it in the Sonder
process environment, then restart. The
OpenAI-compatible and OpenRouter gateways use that platform-trust context
(`openai_compat_gateway.py:144-149`, `adapters/tls_contexts.py:83`). The Sonder
Inference gateway uses it too unless a per-request CA bundle is in force
(`exchange_transport.py:132-136`). The Anthropic gateway does not: it hands
base URL, key and timeout to the Anthropic SDK, which builds its own HTTP
client (`anthropic_gateway.py:750-753`), so Sonder's context is not applied
and trust is decided by the SDK. `requirements-anthropic.txt` allows
`anthropic>=1.1.0,<2` and does not pin httpx2, so the result depends on the
installed httpx2 version (anthropic 1.x requires `httpx2>=2.0.0,<3`). With
httpx2 2.3 or later, which depends on `truststore`, the client trusts the
operating-system store unless `SSL_CERT_FILE` or `SSL_CERT_DIR` is set; if one
is set, that file or directory replaces the operating-system store. With
httpx2 2.0 to 2.2, which depend on `certifi`, the client trusts certifi's
bundled CA list unless those variables replace it, so adding the CA to the
operating-system store does nothing there and only a full-bundle
`SSL_CERT_FILE` works. Check the installed httpx2 version
(`pip show httpx2`) before choosing; for the operating-system store route,
install `httpx2>=2.3`. The
Ollama transport uses the Python default context unless an Ollama CA bundle
is configured, in which case that bundle replaces the default
(`ollama_endpoint.py:338-346`). Because a file or directory holding only
Caddy's root can replace a public-CA trust source, prefer the
operating-system trust store, or an `SSL_CERT_FILE` that is a full CA bundle
with the Caddy root appended; never `SSL_CERT_DIR` with only the root.
There is
no skip-verify option. Exporting Caddy's root CA and the trust-order details
are in the runbook's
[Certificate trust](../runbooks/remote-ollama-caddy.md#certificate-trust)
section; note that its `SONDER_OLLAMA_CA_BUNDLE` and `[ollama].ca_bundle`
settings apply to the Ollama provider only, not to this one.

## Rollback

Unset **every** variable this guide set, then restart Sonder:

```powershell
Remove-Item Env:SONDER_MODEL_BACKEND, Env:SONDER_FAST_PROVIDER, Env:SONDER_GENERAL_PROVIDER, Env:SONDER_CODE_PROVIDER, Env:SONDER_REASONING_PROVIDER, Env:SONDER_VISION_PROVIDER, Env:SONDER_EMBEDDING_PROVIDER, Env:SONDER_OPENAI_BASE_URL, Env:SONDER_OPENAI_MODEL, Env:SONDER_OPENAI_API_KEY, Env:SONDER_OPENAI_EMBED_MODEL, Env:SONDER_OPENAI_ALLOW_HTTP_NETWORKS, Env:SONDER_OPENAI_STREAM_IDLE_TIMEOUT_SECONDS, Env:SONDER_OPENAI_STREAM_FIRST_CONTENT_TIMEOUT_SECONDS -ErrorAction SilentlyContinue
# Only if you set it for this guide and nothing else needs hosted consent:
Remove-Item Env:SONDER_ALLOW_CLOUD -ErrorAction SilentlyContinue
```

```bash
unset SONDER_MODEL_BACKEND SONDER_FAST_PROVIDER SONDER_GENERAL_PROVIDER SONDER_CODE_PROVIDER SONDER_REASONING_PROVIDER SONDER_VISION_PROVIDER SONDER_EMBEDDING_PROVIDER
unset SONDER_OPENAI_BASE_URL SONDER_OPENAI_MODEL SONDER_OPENAI_API_KEY SONDER_OPENAI_EMBED_MODEL SONDER_OPENAI_ALLOW_HTTP_NETWORKS
unset SONDER_OPENAI_STREAM_IDLE_TIMEOUT_SECONDS SONDER_OPENAI_STREAM_FIRST_CONTENT_TIMEOUT_SECONDS
unset SONDER_ALLOW_CLOUD   # only if set for this guide
```

Leave no binding behind: if a `SONDER_<TIER>_PROVIDER` or `SONDER_EMBEDDING_PROVIDER`
set to `openai-compatible` is left in place while `SONDER_OPENAI_BASE_URL` is
removed, every call on that binding fails with "no OpenAI-compatible endpoint
configured" (`openai_compat_gateway.py:349-355`; bindings from
`provider_bindings.py:171-186`). With all of them removed, tiers return to
Ollama (the default).

Removing `SONDER_ALLOW_CLOUD` revokes process-wide hosted consent again
(`domain/cloud_access.py:6-11`) only if `[features].cloud` is not set to true in
`sonder.toml`: `serve` rewrites the variable from typed config
(`sonder_runtime/__main__.py:1139`). A leftover `1` keeps it on for every hosted
provider. A user-level variable set with
`[Environment]::SetEnvironmentVariable(..., 'User')` must be cleared the same
way with a `$null` value, and entries in `/etc/sonder/sonder.env` or a service
unit must be deleted from the file.

## Errors

| Message or symptom | Cause | Fix |
|---|---|---|
| `no OpenAI-compatible endpoint configured (set SONDER_OPENAI_BASE_URL)` | Provider bound, no base URL in the process | Set it; restart |
| `endpoint rejected request (HTTP 404)` | Base URL ends in `/v1` (requests `/v1/v1/...`), or the server has no such route | Remove `/v1` |
| `endpoint rejected request (HTTP 400)` / `422` | Typically an unknown model name, or the server rejects `stream_options` | Set `SONDER_OPENAI_MODEL`; use a server that accepts `include_usage` |
| `endpoint rejected credentials (HTTP 401)` | Wrong or missing key | Set `SONDER_OPENAI_API_KEY` |
| `... is non-loopback but this operation context does not allow cloud` | Hosted endpoint, no consent | `SONDER_ALLOW_CLOUD=1`; never available over A2A |
| `... is not loopback and uses plaintext http; use an https:// base URL, or list its private network in SONDER_OPENAI_ALLOW_HTTP_NETWORKS` | `http://` to a non-loopback host | Use HTTPS, or list the private CIDR and use the IP literal |
| `must not contain credentials (user:password@)` | Secrets in the URL | Use `SONDER_OPENAI_API_KEY` |
| `endpoint is rate limiting requests (HTTP 429)` | Server limit | Retry later |
| `endpoint timed out` | Nothing for 300 s (or the operation deadline) | Check the server; large models may need more time than allowed |
| `cannot reach endpoint` | Server down, wrong host or port | Start it; check the URL |
| Settings changed but nothing happened | Bindings are fixed at startup | Restart Sonder |

## See also

- [Model tiers and gateway](../wiki/08-model-tiers-and-gateway.md): the five
  providers, bindings and the consent map.
- [Sonder Inference provider](../architecture/sonder-inference-provider.md):
  fallback semantics.
- [BACKEND-ATTESTATION](../BACKEND-ATTESTATION.md): diagnostic probe.

## Spend cap and metering

The `openai_compatible` provider (`SONDER_MODEL_BACKEND=openai_compatible` or a
tier bound to it) talks to any OpenAI-compatible server. A **loopback**
endpoint is local and is never metered. A **non-loopback** endpoint (for
example `https://api.openai.com`) is metered: it needs a declared price and
counts against a local daily cap, like the Anthropic provider
(`docs/integration/anthropic.md`).

The Sonder-Inference LAN host and OpenRouter use their own gateways and are not
metered by this setting.

## Variables

| Variable | Default | Meaning |
|---|---|---|
| `SONDER_OPENAI_BASE_URL` | none | Endpoint (https unless a listed private network). |
| `SONDER_OPENAI_API_KEY` | none | Bearer key. |
| `SONDER_OPENAI_MODEL` / `SONDER_OPENAI_EMBED_MODEL` | none | Chat / embedding model ids. |
| `SONDER_OPENAI_PRICE_USD_PER_MTOK` | unset (unpriced) | `<input>,<output>` USD per 1M tokens, each 0..10000. `0,0` declares a free endpoint, for example a private LAN server. |
| `SONDER_OPENAI_EMBED_PRICE_USD_PER_MTOK` | unset (unpriced) | `<input>` USD per 1M tokens for embeddings. |
| `SONDER_OPENAI_DAILY_USD` | `10` | Local daily spend cap in USD, 0..1000000 (`0` refuses every metered call). |
| `SONDER_OPENAI_MAX_TOKENS` | `16000` | Output ceiling for metered calls (1..1000000); a request's `num_predict` is clamped to it. |

There is no built-in price table: prices differ per provider and drift, so you
state them. A metered call with no price is refused before anything is sent.

## How the cap works

- The ledger is `openai-spend.json` in the Sonder state home (per UTC day, shared
  by every process on that home). It holds dollar totals and counts only, never a
  key, a prompt or a reply.
- Before each send the call's worst case (the request's UTF-8 byte length as
  input tokens, plus `max_tokens` of output) is reserved. A call is refused with
  a message naming the cap if that would take the day over it; the refusal lasts
  until 00:00 UTC or until you raise the cap.
- The real cost is taken from the response's `usage` (`prompt_tokens`,
  `completion_tokens`). A response that reports neither is charged the worst case.
- A request that was sent and then failed in any way other than the provider
  answering with an HTTP error (timeout, connection reset, stream stall,
  cancellation, unusable body) is also charged the worst case: spend may be
  over-counted, never under-counted. An HTTP 4xx/5xx answer, and any failure
  before the send (consent, transport policy, admission, cap), is not charged.
