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

## Never do

- Never port-forward 11435 directly.
- Never set `SONDER_HOST=0.0.0.0` "temporarily".
- Never disable the auth-failure limiter to debug a client.
