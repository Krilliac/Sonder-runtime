# Secure remote access

Last reviewed: 2026-11-20 against the code at the docs worktree HEAD.

The reference Sonder deployment never terminates TLS itself or binds the
runtime listener to a non-loopback address. Remote access uses a TLS reverse
proxy in front of that loopback listener. This runbook is the one home for
fronting **Sonder's own HTTP API** (default `127.0.0.1:11435`) with nginx or
Caddy. To front a remote **Ollama worker** with Caddy, see
[remote-ollama-caddy.md](remote-ollama-caddy.md).

## Rules (SPEC-2, non-negotiable)

- `[server].host` stays `127.0.0.1` (default, `sonder_runtime/platform/config.py:151`).
  Set `[server].tls_terminated_by_proxy = true` whenever a proxy is in use. It
  is a typed config key: set it in the TOML file or with
  `--set server.tls_terminated_by_proxy=true`. The typed config loader
  (`sonder_runtime/platform/config.py`) does not read the environment variable
  `SONDER_TLS_TERMINATED_BY_PROXY`: `python -m sonder_runtime serve` takes the
  TOML or `--set` value and exports it to the variable
  (`sonder_runtime/__main__.py:1087`). The direct `serve.py` entrypoint does
  read the variable (`serve.py:866`, `:2339-2340`) and its bind refusal asks
  for `SONDER_TLS_TERMINATED_BY_PROXY=1` (`serve.py:2369-2371`). The setting disables the
  unauthenticated loopback log dashboard and `/v1/local/server-log` (so the
  proxy cannot publish them) and turns on `X-Forwarded-For` handling for the
  peers in `[server].trusted_proxy_cidrs`.
- **This is the one home of the non-loopback bind rule.** The configuration
  loader rejects a non-loopback `[server].host` unless
  `tls_terminated_by_proxy = true` **and** the API key is at least 24
  characters (`config.py:128`, `:1974-1989`), whatever the auth mode. The
  direct entrypoint additionally requires, at bind time, an API key of 24+
  characters for `api-key`, an account secret of 32+ (`MIN_ACCOUNT_SECRET_LENGTH`)
  for `account`, and both for `both`/`either` (`serve.py:2378-2386`). Even then the reference topology
  keeps loopback. The direct entrypoint also refuses `local-open` on a
  non-loopback address (`sonder_runtime/interfaces/http/serve.py:2342-2393`).
  Sonder serves no TLS itself.
- The proxy terminates TLS 1.2+, enforces body limits, forwards only approved
  paths, and replaces inbound forwarding headers.
- Authentication: when credentials are configured, every route except `/live`
  requires them, with two exceptions: `POST /v1/sonder/login` needs no
  credential in `account`/`either` mode, and the first-admin
  `POST /v1/sonder/register` is gated by the bootstrap secret instead. Both
  are throttled. `/live` returns only `{"status":"alive"}`. `/ready`,
  `/health`, `/version` and the metrics path are open without a key only to a
  **loopback peer that also sends a trusted `Host`**
  (`serve.py:5044-5047`, `:5061`). With **no** key and no account requirement
  (`local-open`) there is no authentication at all: every caller is admin and
  developer.
- The API key must be at least 24 characters (`MIN_API_KEY_LENGTH`,
  `config.py:128`). Account modes use a private `SONDER_AUTH_SECRET`.
- Forward only `/live` and `/v1/*`. Behind a loopback proxy every remote
  caller is a `127.0.0.1` peer, so the loopback conveniences on `/ready`,
  `/health`, `/version` and metrics must not be reachable through it.

## When fronting is safe (authenticated mode only)

**Never front a `local-open` listener.** Behind a loopback proxy every remote
caller is a loopback peer, and `local-open` treats every caller as admin and
developer (`serve.py:2034-2035`, `:2055-2063`, `:2461-2467`), including
`POST /v1/permission-mode` (`:6370-6402`) and drain (`:5084-5100`). Adding the
public name to `[server].allowed_hosts` to silence a `421` under `local-open`
would publish an unauthenticated admin API.

Before pointing a proxy at Sonder, confirm all of these:

1. `[server].auth_mode` is `api-key`, `account`, `both` or `either`
   (`config.py:1922`), with an API key of at least 24 characters in the
   secrets file (`SONDER_API_KEY`), or for the account modes a private
   `SONDER_AUTH_SECRET`. Procedure:
   [configure-authentication.md](configure-authentication.md).
2. `[server].tls_terminated_by_proxy = true`.
3. `[server].host` is still `127.0.0.1`.
4. Without the proxy (Windows PowerShell 5.1: use `curl.exe`, because `curl`
   is an alias of `Invoke-WebRequest`), `curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:11435/v1/models`
   prints `401`. A `200` means the listener is open: stop and fix auth first.

Note: `api-key` mode with no key configured on a loopback bind is downgraded
to `local-open` with a warning (`serve.py:1227-1230`), so check step 4
rather than trusting the config alone.

## Setup with nginx

1. Install nginx and real certificates (ACME or internal CA).
2. Copy `packaging/reverse-proxy/nginx-sonder.conf` into
   `/etc/nginx/sites-enabled/`, set `server_name` and cert paths.
3. `nginx -t && systemctl reload nginx`
4. Run the [Verify](#verify) checklist.

The reference file sets `client_max_body_size 1m` (mirrors the 1 MiB default
of `[server].max_request_bytes`), `proxy_read_timeout 300s` and
`proxy_buffering off`, and sends a fixed `Host` of `127.0.0.1:11435` (see
[Host names](../wiki/05-http-api-and-lifecycle.md#host-names-and-421-host_not_allowed)).

## Setup with Caddy

Caddy gets and renews certificates automatically. Install it from the
[official instructions](https://caddyserver.com/docs/install) (Windows:
`winget install CaddyServer.Caddy`; Debian/Ubuntu: the Caddy apt repository
described there).

1. Copy `packaging/reverse-proxy/Caddyfile.sonder-api` to the Caddy config
   location (Linux package default `/etc/caddy/Caddyfile`).
2. Replace the site name `sonder.example.internal` with your public name.
   For a public name with DNS pointing at this host, Caddy obtains a
   certificate by ACME. For a private name, add `tls internal` in the site
   block (the file has it commented); Caddy then signs with its own local CA,
   and clients must trust that CA's root certificate (see the
   [Caddy `tls` directive](https://caddyserver.com/docs/caddyfile/directives/tls)
   and the PKI app docs for where the root is stored).
3. Validate, then load:
   ```bash
   caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile   # "Valid configuration"
   caddy reload   --config /etc/caddy/Caddyfile --adapter caddyfile   # logs "adapted config to JSON"; a non-zero exit means it failed
   ```
   (Under systemd, `systemctl reload caddy` does the same.) On Windows,
   keep the Caddyfile in a folder you own, change the `log { output file ... }`
   path in the Caddyfile (it defaults to `/var/log/caddy/...`) to a writable
   Windows path or remove the `log` block, and run `caddy run --config
   <path> --adapter caddyfile` in a service wrapper or `caddy start`; use
   `curl.exe` for the checks below. `caddy validate` on a config that enables
   `tls internal` creates Caddy's local CA (root and intermediate keys) in
   Caddy's data directory, so run it as the user that will run Caddy.
4. Run the [Verify](#verify) checklist.

How the Caddy recipe interacts with Sonder:

- **Host.** Caddy's `reverse_proxy` passes the client's public `Host` through
  unchanged. Sonder accepts any well-formed name on a credentialed listener
  (`sonder_runtime/interfaces/http/host_policy.py:206-231`), so no
  `allowed_hosts` entry is needed. Because that name is accepted only for
  being credentialed, `/ready`, `/health`, `/version` and metrics need the
  key even from the proxy's loopback address. (Caveat: that holds only when
  the public name is not one of the machine's own names, meaning
  `gethostname`, `getfqdn` or `<host>.local`, and not in `allowed_hosts`
  (`host_policy.py:228-229`). Using the machine's FQDN as the public name makes
  the `Host` trusted; the impact is limited because the reference configs do
  not forward `/ready`, `/health`, `/version` or metrics. `/v1/*` is forwarded
  and includes `/v1/local/server-log`; that path stays disabled only because
  `tls_terminated_by_proxy = true` (`serve.py:680-687`, `:5582-5584`), so never
  front Sonder without that setting.) This is also why fronting
  `local-open` fails with `421 HOST_NOT_ALLOWED`; do not "fix" that by
  adding the name (see above). Full rules:
  [Host names and `421 HOST_NOT_ALLOWED`](../wiki/05-http-api-and-lifecycle.md#host-names-and-421-host_not_allowed).
- **Client address.** With `tls_terminated_by_proxy = true` and the peer in
  `[server].trusted_proxy_cidrs` (default `127.0.0.1/32`, `::1/128`; TOML
  only, `config.py:167`), Sonder reads `X-Forwarded-For` right to left,
  skipping trusted hops (`host_policy.py:244-276`; `serve.py:4988-4999`).
  Caddy replaces an untrusted client's `X-Forwarded-For` with the real peer
  address, so the authentication-failure limiter sees the real client. If
  Caddy runs on a **different** host it cannot reach a loopback listener; that
  is outside the reference topology. It would need a non-loopback
  `[server].host` (all rules above, plus a host firewall allowing only the
  proxy) and the proxy's address in `[server].trusted_proxy_cidrs`.
- **Limits and streaming.** The Caddyfile mirrors the 1 MiB request body
  default; raise both together if you raise `[server].max_request_bytes`
  (valid 1 byte to 16 MiB, `config.py:1929`). Streamed chat sends an SSE
  comment every `[server].stream_heartbeat_seconds` (default 15), so the
  recipe sets `flush_interval -1` to pass frames through at once. Requests may
  take up to `request_timeout_seconds` (300).
- **Origin.** Browser clients send `Origin`; do not reject it on this proxy.
  CORS is answered by Sonder (`[server].cors_origins` or
  `SONDER_CORS_ORIGINS`, exact match; the other grants are in the
  [wiki](../wiki/05-http-api-and-lifecycle.md)).
- **Plain http.** Caddy redirects `http://` to `https://` automatically. The
  thin client refuses to send a key over plain `http` except to loopback and
  never follows redirects when a key is attached
  (`sonder_runtime/adapters/client_request.py:14-37` and
  `sonder_runtime/adapters/client_transport.py:30-55`), so configure clients
  with `https://` URLs.

### Phones and other clients that cannot trust a private CA

`tls internal` (and any other private CA) is for clients you can configure:
desktops, servers and browsers where you install Caddy's root certificate. It
does **not** give you a working Android phone connection. The Sonder app has
no certificate setting of its own: there is no `SecurityContext` or
`badCertificateCallback` anywhere in `app/lib`, and
`scripts/configure_flutter_networking.py:34-70` only switches Android cleartext
traffic on or off, so the app uses the platform's trust store. Android apps
that target API 24 or later ignore user-installed CA certificates unless the
app opts in with a network security configuration, and this app does not
(external platform behaviour, documented by Android; not verified on a device
for this build, so test before you rely on either answer). Treat a private CA
as unsupported for the app and use a certificate the phone already trusts:

1. **A public name with a DNS-01 certificate (recommended for a private
   network).** Own or rent a DNS name, for example `sonder.example.org`, and
   point its A or AAAA record at the PC's private address (an IP in the
   documentation range such as `192.0.2.10` stands in for yours). A private IP
   in public DNS reveals the address but gives no access. Have Caddy obtain a
   Let's Encrypt certificate with the DNS-01 challenge instead of
   HTTP-01, so nothing has to be reachable from the Internet. DNS-01 needs a
   Caddy build that includes the DNS provider module for your registrar
   (`caddy add-package` or `xcaddy`), then in the site block of
   `Caddyfile.sonder-api`:

   ```caddyfile
   sonder.example.org {
       tls {
           dns <provider> {env.<PROVIDER_API_TOKEN>}
       }
       # ... the rest of the shipped site block unchanged ...
   }
   ```

   The exact `dns` syntax depends on the provider module; take it from that
   module's page and from the Caddy
   [`tls` directive](https://caddyserver.com/docs/caddyfile/directives/tls)
   documentation. Keep the token in the Caddy service's environment, not in
   the Caddyfile. In the app, enter `https://sonder.example.org`. A bare IP
   literal cannot use this route: a private IP address cannot receive a
   public certificate, which is why `https://<ip>` in the app's README example
   only works for clients that trust your own CA.
2. **Tailscale Serve (or another tunnel that terminates TLS with a public
   certificate).** Tailscale can serve the PC's loopback port over HTTPS under
   the machine's `<machine>.<tailnet>.ts.net` name with a publicly trusted
   certificate once HTTPS is enabled for the tailnet; Cloudflare Tunnel does
   the same for a Cloudflare-hosted name. Follow that product's own
   documentation for the command, then set `[server].tls_terminated_by_proxy
   = true` as above. The Tailscale and Cloudflare commands were not verified
   for this guide; take the exact invocation from their documentation.
   Whichever you use, the Sonder listener stays on loopback and the key rules
   above are unchanged.
3. **USB or the emulator** (see the app README) for development builds only.

iOS handles user-installed roots differently from Android (they must be both
installed and enabled for full trust in the device settings); this repo has
not tested that route, so it is not documented as supported either.

#### Where the launcher's certificate comes from

The mobile host launcher
([MOBILE_HOST_CONTROL.md](../../MOBILE_HOST_CONTROL.md)) is a **separate
listener on port 11436 that terminates TLS itself**: it does not sit behind
Caddy. It loads a PEM chain and key with `ssl.load_cert_chain` once at startup
(`sonder_launcher.py:2419-2422`; `SONDER_LAUNCHER_CERT` and
`SONDER_LAUNCHER_KEY`, or `--cert` and `--key`). Phones require the same kind
of publicly trusted certificate for it, for the name the phone connects to.
Sources, in order of effort:

- **The certificate Caddy already obtained.** With route 1 above, Caddy keeps
  the certificate and key in its data directory under
  `certificates/<issuer>/<name>/<name>.crt` and `<name>.key`
  ([Caddy data directory](https://caddyserver.com/docs/conventions#data-directory):
  the exact location depends on the OS and the account Caddy runs as). Copy
  or point at those two files; make the key readable only by the account
  running the launcher. The `.crt` already holds the full chain, so it can be
  used as `fullchain.pem`.
- **`tailscale cert <machine>.<tailnet>.ts.net`**, which writes a `.crt` and
  `.key` pair (Tailscale documentation; not verified here).
- **A DNS-01 client of your choice** (for example certbot or acme.sh with your
  DNS provider) for a separate name such as `launcher.example.org`.

The launcher reads the files only at start. After a renewal (Let's Encrypt
certificates last about 90 days; Caddy renews roughly 30 days before expiry)
copy the new files and restart the launcher, or phones start failing with a
certificate error.

Validation status (2026-11-20): `caddy adapt` and `caddy validate` passed on
Caddy 2.9.1 for the shipped Caddyfile (ACME) and with `tls internal` enabled
(validate logs that it is enabling automatic HTTP to HTTPS redirects). Request-level
behaviour was not live-tested: `request_body max_size` answering `413`,
`flush_interval -1`, Host preservation and `X-Forwarded-For` replacement come
from the Caddy documentation. Run the [Verify](#verify) checklist yourself.

## Verify

Run from a remote host (replace the name and export your key; `$KEY` is the
API key or an account token):

```bash
curl -s  https://sonder.example.internal/live                       # 200 {"status":"alive"}
curl -s -o /dev/null -w "%{http_code}\n" https://sonder.example.internal/v1/models   # 401 without key
curl -s -H "Authorization: Bearer $KEY" https://sonder.example.internal/v1/models   # 200 with a model list
curl -s -o /dev/null -w "%{http_code}\n" https://sonder.example.internal/health     # 404 from the proxy
curl -N -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"sonder","stream":true,"messages":[{"role":"user","content":"count to five"}]}' \
  https://sonder.example.internal/v1/chat/completions                # ": keep-alive" lines, then data: frames, arriving incrementally
```

From any host that is not the server, the plaintext port must be closed:

```bash
curl -m 3 http://<server-ip>:11435/live     # must fail to connect (timeout or refused)
```

Failure symptoms:

| Symptom | Likely cause |
|---|---|
| `421 HOST_NOT_ALLOWED` | The listener is `local-open` (no key). Configure authentication; do not add the name. |
| `401` with the right key | Wrong header for the mode. The API key goes in `Authorization: Bearer <key>`; an account token goes in `X-Sonder-Account-Token` (or `Authorization` when no API key is configured). `both` mode needs both headers (`serve.py:2007`, `:2017`). Check the credentials reached the proxy untouched. |
| `413` | Body over the proxy limit (nginx `client_max_body_size`, Caddy `request_body max_size`) or over `[server].max_request_bytes`. |
| Chat arrives all at once, or drops after about a minute | Proxy buffering or idle timeout shorter than the 15 s heartbeat. nginx needs `proxy_buffering off`; Caddy needs `flush_interval -1`. |
| `/health` returns 200 without a key through the proxy | The proxy forwards more than `/live` and `/v1/*`. Remove the extra path immediately. |
| The plaintext check connects | A firewall or `[server].host` mistake. Stop, and set `host = "127.0.0.1"`. |

## Rollback

1. Stop the proxy from serving: nginx `rm /etc/nginx/sites-enabled/<file> && nginx -t && systemctl reload nginx`;
   Caddy `systemctl stop caddy` (or remove the site block and `caddy reload`).
2. Remove the port-443 forward or firewall opening.
3. Re-run the plaintext check above and `curl -m 3 https://<name>/live`; both
   must fail from outside.
4. Optionally set `[server].tls_terminated_by_proxy = false` again (this
   re-enables the loopback log dashboard). Leave authentication on.

## Never do

- Never port-forward 11435 directly.
- Never set `SONDER_HOST=0.0.0.0` "temporarily".
- Never disable the auth-failure limiter to debug a client.
- Never front a `local-open` listener, and never add the public name to
  `allowed_hosts` to get past a `421` there.

## Remote Ollama workers

Remote Ollama workers are a different proxy with different rules, which
live in [remote-ollama-caddy.md](remote-ollama-caddy.md); do not apply the
rules above to them (in particular, do not add authentication on a worker
proxy). Set one up with
[remote-ollama-caddy.md](remote-ollama-caddy.md); the multi-node topology is in
[multi-node-ollama.md](multi-node-ollama.md).
