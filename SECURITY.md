# Security Policy

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
- **read, write, and delete files** (`file_read`, `file_write`, `file_edit`,
  `file_delete`, `directory_create`)
- **fetch from the network** (`web_fetch`, `web_search`)
- **run unattended** (`autopilot_start`, `master_orchestrate`, `loop`)
- **modify its own source** when self-modification is enabled

Those are the product, not a bug. But it means the trust boundary is the
process itself: anything that can send it a request can, in principle, ask
for any of the above. Treat access to a Sonder endpoint as equivalent to
shell access on the host, and size the sandbox accordingly.

Mitigations that are already in place and worth knowing about:

- `file_delete` is **dry-run by default** and requires a confirmation string
  that matches one the tool returned; it also enforces root restrictions and
  a developer-authorization check.
- File operations are constrained to configured roots; escaping them takes an
  explicit `extra_roots` or a bypass token.
- Cloud tiers are **opt-in**. Local tiers run against loopback Ollama, and a
  remote `OLLAMA_HOST` must be explicitly enabled.
- Lessons are passed through a 20-rule privacy classifier before storage, so
  paths, emails, credentials, private keys, and tokens are not distilled into
  memory.

## Deployment guidance

### Loopback (default, recommended)

The default posture binds Ollama to `127.0.0.1` and keeps the runtime local.
Nothing is exposed to the network. If you are evaluating Sonder, stay here.

### Served (`deploy_sonder.sh --serve`)

This mode is genuinely public. It installs a systemd unit with
`SONDER_HOST=0.0.0.0`, generates a random `SONDER_API_KEY`, and prints a
`http://<ip>:<port>/v1` URL. Understand three things before you run it:

1. **The API key is the only authentication.** Anyone who finds the port and
   has the key gets the full tool surface described above.
2. **It speaks plain HTTP.** The key travels in cleartext, so anyone on the
   path — shared wifi, a hop upstream, a compromised router — can capture it
   and replay it. Do not use this mode across an untrusted network as shipped.
3. **The firewall is yours to configure.** The script reminds you; it cannot
   do it for you.

For anything beyond a private LAN, terminate TLS in front of it and never
expose the runtime port directly:

```nginx
server {
    listen 443 ssl;
    server_name sonder.example.com;

    ssl_certificate     /etc/letsencrypt/live/sonder.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/sonder.example.com/privkey.pem;

    location /v1/ {
        proxy_pass http://127.0.0.1:11435/v1/;
        proxy_set_header Host $host;
        proxy_read_timeout 600s;   # generation can be slow
    }
}
```

Then bind the runtime itself to loopback (`SONDER_HOST=127.0.0.1` in the unit
file) so the only route in is through the proxy, and restrict the port at the
firewall or cloud security group.

Rotate `SONDER_API_KEY` if it has ever crossed a network in cleartext.

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
