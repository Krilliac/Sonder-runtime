# Remote Ollama behind a Caddy TLS proxy

Last reviewed: 2026-11-20 against the code at the docs worktree HEAD.

Put a hardened Caddy reverse proxy in front of an Ollama worker so a Sonder
coordinator can use it over HTTPS. This runbook is the one home for the
reference Caddy config and for the coordinator-side rules for **any**
TLS-fronted Ollama endpoint: URL shape, consent, and certificate trust.

- Reference config: [`packaging/reverse-proxy/Caddyfile.ollama-worker`](../../packaging/reverse-proxy/Caddyfile.ollama-worker).
  Validated 2026-11-20 with Caddy v2.9.1: `caddy adapt` and `caddy validate`
  both passed (with `tls internal`, once for a DNS name and once for an IP
  literal), and the adapted JSON keeps the route order described below. No live
  request test of the proxy was run. Run `caddy validate` yourself (step 3 of
  "Worker: install and configure Caddy") after you substitute your values.
- Worker operating-system preparation (Ollama as a service, Linux and Windows):
  [multi-node-ollama.md](multi-node-ollama.md).
- Pool behaviour (circuit breaker, membership, tuning, dedicated embedding
  host): [multi-pc-ollama.md](multi-pc-ollama.md).
- Fronting **Sonder's own HTTP API** with Caddy or nginx is a different job:
  [secure-remote-access.md](secure-remote-access.md#setup-with-caddy).

Placeholders used below: coordinator `10.0.0.1`, worker `<worker-host>`
(a DNS name or an IP literal such as `10.0.0.2`).

## Why a proxy, and what it can and cannot do

Ollama has no authentication and no TLS. Sonder refuses to send prompts to a
non-loopback Ollama over plain HTTP, so a remote worker needs an HTTPS front.

What the proxy **cannot** do: authenticate the coordinator with a password or
token. In static pool mode Sonder sends no credentials at all: no
`Authorization` header, no cookies, no client certificate
(`server.py:5533-5536`, `sonder_runtime/adapters/embeddings.py:550-554`). So
`basic_auth`, bearer-token checks and `forward_auth` on the proxy would answer
401/403 to every probe and the worker would never leave probation. Do not add
them.

What protects the worker instead:

1. TLS, verified by the coordinator against a CA you control.
2. A source-IP allowlist in Caddy (`remote_ip`): only the coordinator.
3. A host firewall or VPN that admits only the coordinator to port 443.
4. An exact method-and-path allowlist, so even the coordinator cannot pull,
   create or delete models through the proxy.

Client-certificate (mTLS) authentication exists only in **external membership**
mode, which is a separate, signed-roster setup:
[multi-node-ollama.md](multi-node-ollama.md#optional-externally-authenticated-membership).
This runbook covers static pool mode.

## Worker: keep Ollama on loopback

On the worker, Ollama must listen on loopback only; Caddy owns the network
listener. Set this in Ollama's own service environment (Ollama setting, not a
Sonder one):

```text
OLLAMA_HOST=127.0.0.1:11434
```

Service setup for Linux and Windows is in
[multi-node-ollama.md](multi-node-ollama.md#1-configure-the-workers-ollama).
`OLLAMA_ORIGINS` is not needed: Sonder is not a browser and sends no `Origin`
header.

Check that nothing listens on the network side of port 11434:

```bash
# Linux
ss -ltn | grep 11434          # expect only 127.0.0.1:11434
```

```powershell
# Windows
Get-NetTCPConnection -LocalPort 11434 -State Listen | Select-Object LocalAddress, LocalPort
# expect only 127.0.0.1
```

## Worker: install and configure Caddy

1. Install Caddy 2 on the worker from the official instructions
   (<https://caddyserver.com/docs/install>). The reference config uses only
   Caddy 2 directives (`route`, `request_body`, `reverse_proxy`, `respond`).
2. Copy `packaging/reverse-proxy/Caddyfile.ollama-worker` to the worker (for
   example `/etc/caddy/Caddyfile`, or any path on Windows) and replace
   `<worker-host>` and `10.0.0.1/32`.
3. Validate, then run or reload:

   ```bash
   caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
   caddy run --config /etc/caddy/Caddyfile --adapter caddyfile     # foreground test
   caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile  # later changes
   ```

   Expected: `Valid configuration` from `validate`. Run Caddy as a service
   (systemd on Linux; a Windows service wrapper on Windows) once the foreground
   test works; see the Caddy install docs.

What each block of the file does:

| Block | Meaning |
|---|---|
| `auto_https disable_redirects` (global) | Sonder never follows redirects (`sonder_runtime/adapters/inference/ollama_endpoint.py:303-308`): any 3xx is an error, never a retry. Do not create an http-to-https redirect. Keep port 80 closed. |
| `https://<worker-host>:443` | Site address. Use the exact name or IP the coordinator will put in its URL: the certificate SAN must match it, and hostname checking is always on (`sonder_runtime/adapters/tls_contexts.py:20`). |
| `tls internal` or a public certificate | `tls internal` makes a private CA inside Caddy and works for IP literals. For a public DNS name, point `tls` at your own certificate and key, or use the DNS-01 ACME challenge (see "Public ACME certificates" below). Do not simply delete the `tls` line: with port 80 closed and 443 limited to the coordinator, ACME issuance cannot complete. |
| `route { ... }` | Children run strictly in the written order, so the denials come before the proxy. |
| 1. `not remote_ip 10.0.0.1/32` -> 403 | Only the coordinator's address is served. |
| 2. `header Origin *` -> 403 | Sonder never sends `Origin`; a request with one is browser traffic. |
| 3. `request_body max_size 32MB` | Sonder's own inbound limit is at most 16 MiB (`[server].max_request_bytes`, default 1 MiB, `sonder_runtime/platform/config.py:1930`), but Sonder adds its own system prompt and context before calling Ollama, and vision requests carry base64 images. 32 MB is a ceiling with headroom, not a measured need; lower it if you never send images. |
| 4. method + path allowlist -> `reverse_proxy 127.0.0.1:11434` | Only the exact routes below. `header_up Host {upstream_hostport}` and `flush_interval -1` are explained after the table. |
| 5. final `respond 403` | Everything else, including `/api/pull`, `/api/create`, `/api/delete`, `/api/copy`, `/api/push`, `/api/embed` and every other method. |

The allowlist is the set of routes Sonder can send to a worker (as of
2026-11-20):

| Method and path | Used for | Where |
|---|---|---|
| `GET /api/version`, `GET /api/tags` | capability probe at admission (one body, at most 1 MiB), preflight and doctor, model catalog | `sonder_runtime/adapters/inference/ollama_pool.py:631-651`, `sonder_runtime/adapters/preflight.py:114`, `server.py:23328` |
| `GET /api/ps` | resident-model check | `server.py:24170` |
| `POST /api/show` | model metadata (body `{"model":..., "name":...}`) | `server.py:1674`, `server.py:23382` |
| `POST /api/chat`, `POST /api/generate` | `/api/chat` is inference; `/api/generate` is model load, unload (`keep_alive: 0`) and prewarm only | `server.py:5278`, `server.py:24572`, `sonder_runtime/application/prefix_cache_report.py:254` |
| `POST /api/embeddings` | embeddings (see the embedding-only variant) | `sonder_runtime/adapters/embeddings.py:548-552` |

Not every deployment sends every route (for example, some residency checks are
made only against a loopback Ollama); the allowlist is the superset, which is
safe because every entry is read-only or inference. If a probe logs a 403 for a
route not listed here, a newer Sonder added it: widen the allowlist with that
exact method and path only.

Why the two non-obvious proxy settings:

- **`header_up Host {upstream_hostport}`.** Caddy forwards the client's `Host`
  header by default. Ollama on loopback rejects a `Host` it does not recognise.
  Tested 2026-11-20 against Ollama 0.40.16 on a loopback listener:
  `Host: worker.example.com` got `403`; `Host: 10.0.0.2:443` and
  `Host: 127.0.0.1:11434` got `200`. A DNS-name site address therefore needs the
  rewrite; keep the rewrite for an IP-literal site address too. Only a private IP
  literal was tested (it got `200` without the rewrite), and the rewrite makes
  the upstream always see the same `Host` whatever the site address is, so it
  never hurts. This Ollama behaviour
  is external and version-dependent; re-test after an Ollama upgrade.
- **No `response_header_timeout`.** Sonder calls Ollama with `stream:false`
  (`server.py:5872`, `:1871`, `:14233`, `:24414`),
  so response headers arrive only after the whole generation. Leave the Caddy
  timeout unset, or keep it at least as long as `SONDER_TIMEOUT` (default 300 s,
  `server.py:629`). Responses up to 16 MiB are read for chat and generate
  (`server.py:4795`).

### Public ACME certificates

The reference config closes port 80 and admits only the coordinator to 443. Under
those rules neither ACME challenge that Caddy uses by default can finish: HTTP-01
needs port 80 reachable from the certificate authority, and TLS-ALPN-01 needs
443 reachable from it. Issuance then fails: Caddy logs ACME errors and has no
usable certificate for the coordinator. Choose one:

- `tls internal` (a private CA; copy its root to the coordinator).
- A certificate and key you supply: `tls /path/cert.pem /path/key.pem`.
- The DNS-01 challenge, which needs no inbound access: a Caddy build that
  includes a DNS provider module and `tls { dns <provider> ... }` (see
  <https://caddyserver.com/docs/automatic-https#dns-challenge>).
- Or open 80 and 443 to the CA for the issuance window. This weakens the
  firewall rule below and is not recommended.

Firewall: allow TCP 443 from the coordinator only, and keep 11434 closed to the
network (it is already loopback-only).

```bash
# Linux (ufw)
sudo ufw allow from 10.0.0.1 to any port 443 proto tcp
```

```powershell
# Windows (elevated)
New-NetFirewallRule -DisplayName "Ollama TLS proxy (coordinator only)" `
  -Direction Inbound -Protocol TCP -LocalPort 443 -RemoteAddress 10.0.0.1 -Action Allow
```

### Embedding-only host

For a host that serves only Sonder's embeddings (`SONDER_EMBED_BASE_URL`,
[multi-pc-ollama.md](multi-pc-ollama.md#dedicated-embedding-host)), narrow step 4
of the Caddyfile to what the embedder sends: `GET /api/tags` (model revision
check, `sonder_runtime/adapters/embeddings.py:233`), `GET /api/version` (curl
check) and `POST /api/embeddings`:

```caddyfile
@ollama_read {
	method GET
	path /api/version /api/tags
}
@ollama_write {
	method POST
	path /api/embeddings
}
```

The embedding host is not in the worker pool: it has no membership or
probation, and a 502-504 from the proxy opens the embed cooldown
(`SONDER_EMBED_COOLDOWN_SECONDS`, default 30 s,
`sonder_runtime/adapters/embeddings.py:30-37`, `:735-741`). The coordinator rules in
the next section apply to it unchanged.

## One host that is both the Ollama worker and a compute node

A spare PC can serve remote Ollama inference to the coordinator's pool **and**
run Sonder as a compute worker
([compute-fabric.md](compute-fabric.md)). Two reference Caddyfiles then collide:
`Caddyfile.ollama-worker` and `Caddyfile.sonder-api` both put their site on
`:443`, so pasting them into one file fails with a duplicate site address, and
only the Ollama file has the global `auto_https disable_redirects` block. Run
**one Caddy with one config and two sites on distinct ports**. Nothing else
changes: Ollama stays on `127.0.0.1:11434`, the Sonder API on
`127.0.0.1:11435`, and the two upstreams never share a path.

| Site | Port used here | Upstream | Coordinator-side setting |
|---|---|---|---|
| Ollama (inference) | 11443 | `127.0.0.1:11434` | `[ollama].workers = ["https://<worker-host>:11443"]` |
| Sonder API (compute) | 8443 | `127.0.0.1:11435` | `[[compute.nodes]].origin = "https://<worker-host>:8443"` |

Any two distinct free ports work; these are the values the two runbooks already
use ([multi-node-ollama.md](multi-node-ollama.md) and
[compute-fabric.md](compute-fabric.md#front-a-worker-with-a-tls-proxy)). The
controller requires an explicit port in `origin`, and a worker URL must carry
the same port as the site.

Build the file from the two references. This recipe has not been run through
`caddy validate` (there was no Caddy on the machine that wrote it), so run
`caddy validate --config <file> --adapter caddyfile` before you load it:

```caddyfile
{
	# Needed by the Ollama site: Sonder never follows a redirect and only talks
	# https. It applies to every site in this file, including the API site (see
	# "Behaviour that changes when the two files are merged" below).
	auto_https disable_redirects
}

https://<worker-host>:11443 {
	tls internal
	# ... the rest of the site block of Caddyfile.ollama-worker, unchanged:
	# log, then the route { ... } with the remote_ip allowlist, Origin refusal,
	# 32MB body limit, the two matchers, header_up Host and the final 403.
}

https://<worker-host>:8443 {
	tls internal
	# ... the rest of the site block of Caddyfile.sonder-api, unchanged:
	# request_body 1MiB, then route { @sonder path /live /v1/* ... respond 404 },
	# and its log block with an output path that exists on this host.
}
```

Copy each site's body from its reference file as it is; change only the site
address line (the port) and keep the same `tls` choice on both sites. Use the
Ollama file's global block as the only global block.

Behaviour that changes when the two files are merged:

- **Redirects.** The global `auto_https disable_redirects` now covers the API
  site too. `Caddyfile.sonder-api` on its own lets Caddy redirect plain-http
  requests to https; in the combined file it does not, so an `http://` request
  to the API fails to connect or is not redirected. The Sonder clients and the
  controller use `https://` URLs and refuse redirects once a key is attached,
  so this only affects a person typing `http://` in a browser. Keep port 80
  closed.
- **Certificates and ACME.** With `tls internal` on both sites, one Caddy local
  CA signs both, so the coordinator needs **one root**. A public ACME name on
  the API site needs the DNS-01 challenge (or a supplied `tls <cert> <key>`
  pair), exactly as for the Ollama site: a non-443 site port does not move the
  HTTP-01 and TLS-ALPN-01 challenges off ports 80 and 443 (Caddy documentation;
  not run here), and port 80 is closed. See
  [Public ACME certificates](#public-acme-certificates) and
  [secure remote access](secure-remote-access.md#phones-and-other-clients-that-cannot-trust-a-private-ca)
  for DNS-01.
- **Trust on the worker itself.** `tls internal` installs the CA root into the
  worker's own trust store unless the global block also has `skip_install_trust`
  (the Ollama reference file comments that option). The coordinator is a
  different machine, so the worker's own store does not matter to it.
- **Per-site rules stay per-site.** The Ollama site's `remote_ip` allowlist and
  `header_up Host {upstream_hostport}` do not apply to the API site. The API
  site is protected by the Sonder API key, and its `Host` is passed through
  unchanged. If you also want the source-IP restriction on it, add the same
  `not remote_ip <coordinator>/32` then `respond 403` step at the top of its
  `route`.
- **Firewall.** Allow both ports from the coordinator only (the commands in
  [Public ACME certificates](#public-acme-certificates) show one port; repeat
  them for 8443), and keep 11434 and 11435 closed to the network.

Verify each lane from the coordinator: the Ollama checks in [Verify](#verify),
then `curl --cacert <ca-file> https://<worker-host>:8443/live` (`200`,
`{"status":"alive"}`) and the compute checks in
[compute-fabric.md](compute-fabric.md#front-a-worker-with-a-tls-proxy). The
coordinator must trust the one root for both lanes, and that is not automatic:
see [Coordinator trust for both lanes](#coordinator-trust-for-both-lanes).

## Coordinator settings

This is the one home for pointing Sonder at any TLS-fronted Ollama endpoint.
Restart Sonder after any change below.

### Worker entry or remote primary

- **Worker entry (recommended).** Keep the coordinator's own Ollama on
  loopback as the primary and add the remote one as a pool worker.
- **Remote primary.** Set `[ollama].url` (or `OLLAMA_HOST`) to the remote
  HTTPS origin. Same URL rules; see the caveats at the end of this section.

### The settings

TOML (`sonder.toml`):

```toml
[ollama]
allow_remote = true
workers = ["https://<worker-host>:443"]
ca_bundle = "/etc/sonder/ollama-ca.pem"
```

Environment equivalent (the environment overrides the file):

```bash
export SONDER_ALLOW_REMOTE_OLLAMA=1
export SONDER_OLLAMA_WORKERS=https://<worker-host>:443
export SONDER_OLLAMA_CA_BUNDLE=/etc/sonder/ollama-ca.pem
```

```powershell
$env:SONDER_ALLOW_REMOTE_OLLAMA = "1"
$env:SONDER_OLLAMA_WORKERS = "https://<worker-host>:443"
$env:SONDER_OLLAMA_CA_BUNDLE = "$env:USERPROFILE\sonder\ollama-ca.pem"
```

Rules, each enforced in code at startup (a violation fails closed with the
quoted message):

| Rule | Detail | Error text | Code |
|---|---|---|---|
| Consent | Any non-loopback endpoint needs `[ollama].allow_remote = true` or `SONDER_ALLOW_REMOTE_OLLAMA` set to `1`, `true`, `yes` or `on`. | `non-loopback OLLAMA_HOST is blocked because prompts and embeddings would leave this machine; set SONDER_ALLOW_REMOTE_OLLAMA=1 to opt in explicitly`; for workers `remote worker endpoints require SONDER_ALLOW_REMOTE_OLLAMA=1`, or `[ollama].workers remote entries require the remote-Ollama consent gate`; for a TOML `[ollama].url`, `[ollama].url host '...' is not loopback and allow_remote is false (remote-Ollama consent gate)` | `sonder_runtime/domain/ollama_policy.py:17-22, :162-169`; `sonder_runtime/adapters/inference/ollama_pool.py:358-360`; `sonder_runtime/platform/config.py:2125-2128`; `config.py:2075-2081` |
| `https` | Remote endpoints must use `https`. | `non-loopback OLLAMA_HOST must use https to protect prompts and embeddings in transit`; `remote worker endpoints must use https`; `[ollama].workers remote entries must use https`; for a TOML `[ollama].url`, `[ollama].url remote Ollama must use https so prompts and embeddings are protected in transit` | `ollama_policy.py:170-174`; `ollama_pool.py:361-363`; `config.py:2129-2130`; `config.py:2082-2087` |
| Explicit port | Always write the port, even `:443`. | `OLLAMA_HOST must include an explicit port`; `[ollama].url must include an explicit port`; `[ollama].workers entry '...' must include a host and explicit port` | `ollama_policy.py:156-157`; `config.py:2071-2072, :2113-2117` |
| Origin only | `scheme://host:port`: no path, query, fragment or `user:pass@`. | `[ollama].workers entries must be origins without paths, queries, or fragments`; `[ollama].workers entries must not contain inline credentials` | `config.py:2109-2122` |
| Unique roster | A worker equal to the primary after normalisation is rejected, as is a repeat. The primary plus workers may not exceed `worker_pool_max_workers` (default 16). | `[ollama].workers contains a worker that duplicates primary after canonical normalization` | `config.py:2138-2158` |

`0.0.0.0` and `::` are rewritten to loopback, not treated as remote
(`ollama_policy.py:92-99`).

### Environment auto-prefix pitfall

`SONDER_OLLAMA_WORKERS` is split on `,` or `;`, and an entry **without `://`
gets `http://` prepended**. Likewise a bare `OLLAMA_HOST=host:443` becomes
`http://host:443` (`sonder_runtime/platform/config.py:1616-1628`). A bare
`<worker-host>:443` therefore fails the https rule above even though you meant
TLS. Always write `https://` explicitly in both variables.

### Certificate trust

The coordinator verifies the proxy's certificate and its hostname on every
request; there is no insecure-skip-verify option. Trust anchors, first match
wins (`sonder_runtime/platform/config.py:1478-1496, :1642-1645`;
`sonder_runtime/adapters/inference/ollama_endpoint.py:141-158`):

1. `[ollama].ca_bundle` or `SONDER_OLLAMA_CA_BUNDLE` (the environment wins).
2. Otherwise the first absolute, existing file named by `SSL_CERT_FILE`, then
   `REQUESTS_CA_BUNDLE`.
3. Otherwise the operating-system trust store.

A configured bundle **replaces** the system store; it does not add to it
(`sonder_runtime/adapters/tls_contexts.py:96-103`). If the coordinator must
trust several private CAs (for example two workers with different Caddy roots),
concatenate their PEM files into one bundle file:

```bash
cat caddy-root-worker1.crt caddy-root-worker2.crt > /etc/sonder/ollama-ca.pem
```

```powershell
Get-Content worker1-root.crt, worker2-root.crt | Set-Content "$env:USERPROFILE\sonder\ollama-ca.pem"
```

For `tls internal`, the file to copy is the worker's
`<caddy data dir>/pki/authorities/local/root.crt` (find the directory with
`caddy environ`, entry `caddy.AppDataDir`). Copy only the root certificate,
never a key.

The bundle path must be absolute and name an existing file, or startup fails
(`[ollama].ca_bundle must be an absolute path`, `... must name an existing
regular file`, `sonder_runtime/platform/config.py:2089-2095`; the transport
error is `Ollama CA bundle must be an existing absolute file`,
`ollama_endpoint.py:136, :149`). The bundle is re-read on every request, so
replacing the file with a rotated root takes effect without a restart
(`tls_contexts.py:10-12`); changing the path needs a restart.

Transport behaviour to know: Sonder ignores `HTTP_PROXY`/`HTTPS_PROXY`
environment variables for Ollama calls and never follows redirects
(`ollama_endpoint.py:303-308`).

### Coordinator trust for both lanes

The pool lane (`[ollama].workers`) and the compute lane
(`[[compute.nodes]].origin`) verify certificates through **different trust
sources** in the same coordinator process, so trusting a worker's Caddy root for
one lane does not trust it for the other:

| Lane | Trust source | Where |
|---|---|---|
| Ollama pool | the Ollama bundle: `SONDER_OLLAMA_CA_BUNDLE` or `[ollama].ca_bundle`; if neither is set, the first absolute existing file named by `SSL_CERT_FILE`, then `REQUESTS_CA_BUNDLE`; else the OS store. A bundle **replaces** the OS store for this lane. | `platform/config.py:1478-1496`, `:1642-1645`; `adapters/tls_contexts.py:96-103` |
| Compute | the default context: the OS store plus `SSL_CERT_FILE` / `SSL_CERT_DIR` read from the **process environment**. There is no compute CA setting. | `adapters/tls_contexts.py:51-90`; [compute-fabric.md](compute-fabric.md#tls-trust-on-the-controller) |

For one private root (the combined worker, or several workers behind one Caddy
CA), pick one of these:

1. **Explicit and independent (recommended).** Copy
   `<caddy data dir>/pki/authorities/local/root.crt` from the worker to the
   coordinator, then:
   - set `[ollama].ca_bundle = "<absolute path>/worker-root.crt"` (or
     `SONDER_OLLAMA_CA_BUNDLE`) for the pool lane. List every private root the
     pool needs in this one file, because it replaces the OS store;
   - install the same root into the coordinator's OS trust store for the
     compute lane (Windows, elevated: `certutil -addstore -f Root worker-root.crt`;
     Debian or Ubuntu: copy it to `/usr/local/share/ca-certificates/` with a
     `.crt` extension and run `update-ca-certificates`; other systems: their own
     tooling). These are the operating systems' standard commands and were not
     run here. The OS store change is picked up within 5 minutes
     (`tls_contexts.py`, `_MAX_AGE_SECONDS`).
2. **One file through `SSL_CERT_FILE`.** Point `SSL_CERT_FILE` at an absolute PEM
   file that contains the root, in the environment of the coordinator
   **process** (a service `Environment=` line or the scheduled task's
   environment, not only a shell you opened). Compute reads it, and the Ollama
   lane adopts it as its bundle when `[ollama].ca_bundle` is unset. Two traps:
   a value that is relative or names a missing file is skipped without an error
   (`process_ca_bundle`), so the pool lane silently falls back to the OS store
   and then fails verification; and on Linux OpenSSL treats `SSL_CERT_FILE` as
   the replacement for the system CA file, so the file must also contain the
   public roots the coordinator still needs (on Windows Python also loads the OS
   store; OpenSSL and Python behaviour, not run here). Prefer option 1 unless
   you want exactly one file.

Do not set `SSL_CERT_FILE` for the compute lane's benefit and forget its effect
on the Ollama lane: with `[ollama].ca_bundle` unset it silently becomes the
Ollama bundle, so any other remote Ollama worker with a publicly trusted
certificate stops verifying unless its CA is also in that file. Setting
`[ollama].ca_bundle` explicitly ends that coupling.

Check each lane separately. `curl --cacert <root> https://<worker-host>:11443/api/version`
and `https://<worker-host>:8443/live` prove the proxy and the name, not Sonder's
trust. Prove Sonder's side with `doctor` (Ollama reachability) for the pool lane
and `POST /v1/compute/nodes/refresh` (admin credential) then
`GET /v1/compute/nodes` for the compute lane.

### Remote primary caveats

- Vision analysis and fanout synthesis refuse a non-loopback primary
  (`vision analysis requires a loopback Ollama endpoint`,
  `sonder_runtime/adapters/inference/ollama_vision.py:88-89`;
  `fanout synthesis requires a loopback Ollama endpoint`, `server.py:24349-24350`).
  This is another reason to prefer a loopback primary plus a remote worker
  entry.
- Training promotion evaluation requires a loopback Ollama (`promotion_eval.py:356-363`);
  a remote primary blocks it. See
  [TRAINING.md](../../TRAINING.md#remote-or-caddy-fronted-ollama).
- `/ready` can report the primary unavailable although chat works: see
  "Known limitation: `/ready` with a remote primary" under Troubleshooting.

## Verify

Run these from the coordinator unless noted. `root.crt` is the CA file you
gave Sonder (skip `--cacert` for a publicly trusted certificate).

```bash
# 1. Allowed routes: expect 200 and JSON
curl --cacert root.crt https://<worker-host>:443/api/version
curl --cacert root.crt https://<worker-host>:443/api/tags

# 2. Allowed POST reaches Ollama: expect 404 (Ollama: model not found), not 403
echo '{"model":"no-such-model","name":"no-such-model"}' > show.json
curl --cacert root.crt -H "Content-Type: application/json" \
  --data-binary @show.json https://<worker-host>:443/api/show

# 3. Denied routes: expect 403 from Caddy
curl --cacert root.crt -X POST https://<worker-host>:443/api/pull -d '{}'
curl --cacert root.crt -X DELETE https://<worker-host>:443/api/delete
curl --cacert root.crt -H 'Origin: https://example.com' https://<worker-host>:443/api/tags

# 4. From a host NOT in remote_ip: expect 403 (or a timeout if the firewall drops it)
curl --cacert root.crt https://<worker-host>:443/api/tags

# 5. From any other machine: Ollama itself must not be reachable. Expect
#    connection refused or a timeout.
curl --max-time 5 http://<worker-host>:11434/api/tags
```

On Windows PowerShell use `curl.exe` (plain `curl` is an alias for
`Invoke-WebRequest`). Single-quoted JSON passed inline is mangled by Windows
PowerShell 5.1, and `echo '...' > show.json` there writes UTF-16 with a BOM (not
JSON, so the expected 404 becomes a 400). Create the file like this instead:

```powershell
Set-Content -Encoding ascii show.json '{"model":"no-such-model","name":"no-such-model"}'
```

Then ask Sonder (restart it first):

```bash
python -m sonder_runtime preflight
python -m sonder_runtime doctor
```

(Both commands accept `--config <path-to-sonder.toml>` if your file is not in
the default place, plus `--secrets`, `--set`, `--json` and `--skip-ollama`;
`sonder_runtime/__main__.py:1849-1861, :1872-1880`.) Expected:

- `preflight` lists `ollama` for the primary and one `ollama_worker_<n>` per
  worker, each `<host>: <n> models`
  (`sonder_runtime/adapters/preflight.py:114-135, :150-180`). A bad bundle adds
  a failed, non-required `ollama_ca_bundle` check with the error text
  (`preflight.py:300-304`).
- `doctor` reports `ollama` and `ollama_workers`; a failure shows the transport
  reason, and a bad bundle shows `[ollama].ca_bundle: ...` inside those lines
  (`sonder_doctor.py:453-464, :524-565`). There is no separate `ollama_ca_bundle`
  line in `doctor`.
- After `serve`, `repl` or `mcp` starts, the log shows
  `inference membership: members=N active=N probation=N unhealthy=N expired=N`
  (`sonder_runtime/application/inference_membership/controller.py:180`). A new
  worker stays in `probation` and carries no traffic until the first controller
  pass succeeds (first pass at start, then every `min(30,
  worker_capability_ttl_seconds / 2)` seconds, minimum 1;
  `sonder_runtime/bootstrap/app.py:2330-2348`, `:602`). A roster with no active
  member logs at warning level.
- The pool admin status (MCP tool `ollama_pool_admin_status`, `server.py:23943`,
  which needs the admin token) reports `tls_verification` as
  `configured-ca-bundle` or `system-trust-store`; also `invalid-ca-bundle` when
  the configured bundle is unusable (`ollama_endpoint.py:157-158`),
  `not-applicable` when there are no remote workers, and `pinned-ca-exact-san`
  in external membership mode (`ollama_pool.py:2610-2617`).

## Troubleshooting

| Symptom (exact text) | Cause | Fix |
|---|---|---|
| `non-loopback OLLAMA_HOST is blocked because prompts and embeddings would leave this machine ...`, `[ollama].url host '...' is not loopback and allow_remote is false ...` | No consent. | Set `allow_remote = true` or `SONDER_ALLOW_REMOTE_OLLAMA=1`. |
| `non-loopback OLLAMA_HOST must use https ...`, `remote worker endpoints must use https`, `[ollama].workers remote entries must use https`, `[ollama].url remote Ollama must use https ...` | `http://` URL, or an env entry with no scheme that got `http://` prepended. | Write `https://` explicitly. |
| `OLLAMA_HOST must include an explicit port`, `must include a host and explicit port` | Port missing. | Add `:443`. |
| `[ollama].workers entries must be origins without paths, queries, or fragments` | URL has a path such as `/api`. | Origin only: `https://<worker-host>:443`. |
| `[ollama].workers contains a worker that duplicates primary ...` | Worker equals the primary after normalisation. | Remove the duplicate. |
| `[SSL: CERTIFICATE_VERIFY_FAILED] ... self signed certificate` or `unable to get local issuer certificate` (shown in preflight/doctor detail, worker error category `tls`) | The CA that signed the proxy certificate is not in the trust anchors above. Remember a configured bundle replaces the system store. | Put the right root PEM (all roots, concatenated) in `ca_bundle`; check `tls_verification`. |
| `certificate verify failed: Hostname mismatch` or `IP address mismatch` | Certificate SAN does not contain the name or IP in the coordinator's URL. | Use the same name in the Caddy site address and the URL; for `tls internal` on a new IP, change the site address and reload. |
| `Ollama CA bundle must be an existing absolute file`, `[ollama].ca_bundle must be an absolute path` | Relative or missing path. | Use an absolute path to an existing file. |
| `timed out` / `Connection refused` / worker stays in `probation` | Port 443 blocked, Caddy down, or the 2 s probe budget (`worker_probe_timeout_ms`, default 2000, `config.py:235`) is too short for a slow link. | Check the firewall and `caddy` status; raise `[ollama].worker_probe_timeout_ms`. |
| `HTTP Error 403: Forbidden` | Caddy denial (source IP, `Origin`, or route not in the allowlist), or Ollama rejecting the `Host` header. | Check the `remote_ip` value is the coordinator as the worker sees it; check `header_up Host`; read Caddy's log. A 403 on `/api/version` or `/api/tags` is a hard probe error. |
| `HTTP Error 401` / `404` / `301`/`302` | Auth added to the proxy; wrong path or Ollama missing the route; a redirect rule (for example a port-80 redirect). | Remove proxy auth; remove redirects. These are hard errors, never failover. (A `404` on `/api/version` alone is tolerated at probe time, `ollama_pool.py:633-641`.) |
| ACME errors in Caddy's log, or no usable certificate right after the `tls` line was removed | Caddy tried to obtain a public certificate while port 80 is closed and 443 is coordinator-only, so neither HTTP-01 nor TLS-ALPN-01 can complete. | See "Public ACME certificates": `tls internal`, your own pair, or DNS-01. |
| `HTTP Error 413` | Request larger than `request_body max_size`. | Raise it (for vision requests with big images). |
| `HTTP Error 502`, `503`, `504` | Caddy cannot reach Ollama (stopped or restarting), or a proxy timeout. | Start Ollama; if 504 on long generations, remove any `response_header_timeout` shorter than `SONDER_TIMEOUT`. |
| `timed out waiting for Ollama worker capacity` | Local admission queue full or waiting too long (`worker_max_inflight`, `worker_queue_depth`, `worker_admission_timeout_ms`), not a proxy fault (`ollama_pool.py:1917`). | Tune the pool: [multi-pc-ollama.md](multi-pc-ollama.md). |

Status-code behaviour (`sonder_runtime/adapters/inference/ollama_pool.py:61,
:1120-1130`): **502, 503 and 504** from the proxy count as transport failures.
They count toward the worker's circuit (opens after
`worker_failure_threshold` = 3 consecutive failures, cooldown
`worker_cooldown_seconds` = 30, `config.py:232-233`). Requests marked idempotent
are replayed on the next worker on 502-504 and on connection errors or
timeouts. Which calls are replayed is owned by
[multi-pc-ollama.md](multi-pc-ollama.md); read it there. The hazard specific to
a proxy: a Caddy 504 or a dropped connection during a long `stream:false`
generation can make Sonder resend the same request to another member while the
first Ollama is still generating, so two workers may do the work. **401, 403, 404 and any 3xx** are hard
errors: no failover, no retry. For embeddings, 502-504 open the embed cooldown
instead.

### Known limitation: `/ready` with a remote primary

Code reading only, **not runtime-tested** (2026-11-20). The `/ready`
dependency probe (`sonder_runtime/adapters/web/lifecycle.py:658-669`, URL from
`_ollama_url()` at `:1269-1271`) calls `urllib.request.urlopen` on `OLLAMA_HOST`
directly, not through the Ollama transport. It therefore (a) uses the system
trust store, ignoring `ca_bundle`, and (b) honours `HTTP_PROXY`. With a remote
HTTPS **primary** whose certificate comes from a private CA, the probe fails and
`/ready` can answer `required dependency ollama is unavailable` while chat works
(`sonder_runtime/platform/service_state.py:240`). A loopback primary with remote
**workers** is not affected, because only the primary is probed. Workaround:
keep the primary on loopback, or use a certificate the system store trusts. This
should be fixed in code; it is reported as a defect.

## Rollback

Nothing is exposed to the network except Caddy, and Ollama stays on loopback.

1. Worker: restore the previous Caddyfile and `caddy reload`, or stop Caddy
   (`caddy stop` works only for an instance started with `caddy start` or
   `caddy run`; for a service, stop the service). Close port 443 in the firewall.
2. Coordinator: remove the worker from `[ollama].workers` (or
   `SONDER_OLLAMA_WORKERS`), set `allow_remote = false` if nothing else is
   remote, and restart Sonder.
3. Confirm with `python -m sonder_runtime preflight`: only the loopback
   `ollama` check remains among the Ollama checks (preflight also runs state,
   disk, schema and policy checks, `preflight.py:293-297`).

## Related

- Sonder's own API behind Caddy:
  [secure-remote-access.md](secure-remote-access.md#setup-with-caddy).
- Sonder Inference's own Ollama client has different limits: it has no
  CA-bundle option, so a Caddy internal CA works only if its root is in the
  operating-system trust store (untested). See `docs/SETUP.md`, section "Remote
  Ollama from Sonder-Inference", in the Sonder-Inference repository (it may not
  be on that repository's main branch yet).
- Every `[ollama]` key and bound: [wiki 03 configuration](../wiki/03-configuration.md).
