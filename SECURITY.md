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
- Structured `data_query` reads are bounded and side-effect-free: SQLite uses
  a read-only URI and deny-by-default authorizer, while JSON/JSONL/CSV/TSV use
  exact structured filters and projections without expression evaluation.
- Cloud tiers are **opt-in**. Local tiers run against loopback Ollama, and a
  remote `OLLAMA_HOST` must be explicitly enabled.
- Lessons are passed through a 20-rule privacy classifier before storage, so
  paths, emails, credentials, private keys, and tokens are not distilled into
  memory.

## Deployment guidance

### Loopback (default, recommended)

The default posture binds Ollama to `127.0.0.1` and keeps the runtime local.
Nothing is exposed to the network. If you are evaluating Sonder, stay here.

### Local development service (`deploy_sonder.sh --serve`)

This convenience mode installs a loopback-only systemd service. It generates a
random `SONDER_API_KEY`, stores it in `/etc/sonder/sonder-local.env` with mode
0600, and prints a `http://127.0.0.1:<port>/v1` URL for local clients. It does
not provide a supported remote deployment and must not be port-forwarded.

Older revisions of this script bound `0.0.0.0` and advertised a plaintext
public URL. Upgrade before using it, remove any firewall rule that exposed port
11435, and rotate the API key if it ever crossed a network in cleartext.

### Remote access

Install the [server-private profile](docs/runbooks/install-server-private.md),
keep its runtime listener on `127.0.0.1`, and follow the
[secure remote access runbook](docs/runbooks/secure-remote-access.md). The
reference nginx configuration terminates TLS, limits requests, and forwards to
the loopback listener. Never expose the runtime port directly: possession of a
valid credential grants access to the runtime's powerful tool surface.

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
