# Secure remote access

The reference Sonder deployment never terminates TLS itself or binds the
runtime listener to a non-loopback address. Remote access uses a TLS reverse
proxy in front of that loopback listener.

## Rules (SPEC-2, non-negotiable)

- `[server].host` stays `127.0.0.1`. Set `tls_terminated_by_proxy = true`
  whenever the reference proxy is in use: this disables the unauthenticated
  loopback log dashboard before the proxy can publish it. The configuration
  loader also rejects a non-loopback bind without that declaration **and** a
  strong API key — and even then the reference topology keeps loopback.
- The proxy terminates TLS 1.2+, enforces body limits, forwards only
  approved paths, and strips inbound forwarding headers.
- `/live` may bypass auth (returns only `{"status":"alive"}`). Everything
  else requires the bearer key; `/ready`, `/health`, `/metrics`,
  `/version` are additionally open to loopback peers only.

## Setup

1. Install nginx and real certificates (ACME or internal CA).
2. Copy `packaging/reverse-proxy/nginx-sonder.conf` into
   `/etc/nginx/sites-enabled/`, set `server_name` and cert paths.
3. `nginx -t && systemctl reload nginx`
4. Verify from a remote host:
   ```bash
   curl -s https://sonder.example.internal/live          # 200, alive
   curl -s https://sonder.example.internal/v1/models      # 401 without key
   curl -s -H "Authorization: Bearer $KEY" https://sonder.example.internal/v1/models
   ```
5. Confirm the plaintext port is unreachable remotely:
   `curl -m 3 http://<server-ip>:11435/live` must fail to connect.

## Private-network Ollama workers (trusted_origins)

For Ollama worker pools on a physically isolated LAN (direct Ethernet,
isolated VLAN), `[ollama].trusted_origins` accepts CIDR ranges where HTTP
workers are allowed without TLS.  This does **not** relax the Sonder
listener rules above — it only affects outbound connections to Ollama
workers in the pool.  See `multi-node-ollama.md` for the full setup.

```toml
[ollama]
allow_remote = true
workers = ["http://10.77.0.2:11434"]
trusted_origins = ["10.77.0.0/24"]
```

On shared or routable networks, use the TLS proxy path instead and omit
`trusted_origins`.

## Never do

- Never port-forward 11435 directly.
- Never set `SONDER_HOST=0.0.0.0` "temporarily".
- Never disable the auth-failure limiter to debug a client.
