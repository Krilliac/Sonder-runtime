# Sonder Runtime thin client (`sonder_client.py`)

> **Contract scope:** this focused contract describes current behavior. Unfinished
> implementation work is tracked in the
> [master implementation specification](docs/architecture/SONDER-MASTER-IMPLEMENTATION-SPEC.md); the
> [behavior status](#behavior-status) table labels what is implemented,
> experimental, proposed, degraded, or unsupported.

`sonder_client.py` is a thin remote client. It runs from a checkout of this
repository with only the Python standard library plus the checkout's own
`sonder_runtime` client adapters: no pip installs, no Ollama, and no `mcp`
package. It is **not** a single-file download; copied on its own it fails
with `ModuleNotFoundError: No module named 'sonder_runtime'`. Point it at a
Sonder Runtime host elsewhere. Remote hosts use the
[server-private installer](docs/runbooks/install-server-private.md) and a
[TLS reverse proxy](docs/runbooks/secure-remote-access.md); the runtime listener
itself stays on loopback.

Sonder Runtime is orchestration software, not a full or base model. A local
host normally runs Ollama separately to store/load model weights and perform
inference; Sonder supplies the API, prompting, memory, tools, policy, training,
and deployment loop around it. The thin client needs neither Ollama nor model
weights because those stay on the host.

## Three ways to use Sonder Runtime

1. **Hosted server + thin client (this doc).** Someone else (or your own
   VPS) runs Sonder's full orchestration loop and its configured inference
   host; you talk to it over HTTPS from any PC with Python and a checkout of
   this repository. No local GPU, no Ollama, and no pip installs are needed on
   the client side.
2. **Fully local.** Clone the repo and run Sonder Runtime on your own
   machine — the `sonder` REPL / `sonder.cmd` (Windows). See
   [README.md → Quick start](README.md#quick-start).
3. **Integrated with Claude Code, via MCP.** `server.py` is registered as
   the `sonder-runtime` MCP server; Claude Code calls `sonder(...)`,
   `offload(...)`, etc. directly as tools. See [README.md →
   Why Sonder](README.md#why-sonder).

This doc covers #1.

## Requirements

**Python 3** (any recent 3.x) and a checkout of this repository (a Git clone
or an extracted source archive). The client imports only the standard library
and the checkout's `sonder_runtime` package, so no virtual environment, pip
installs, or Ollama are required.

## Get the client

Clone the repository (a shallow clone is enough) and run the client from it:

```bash
git clone --depth 1 https://github.com/Krilliac/Sonder-runtime.git
cd Sonder-runtime
python3 sonder_client.py --help
```

On Windows, use `py sonder_client.py --help` from the same directory. Keep
`sonder_client.py` inside the checkout; it must sit beside the `sonder_runtime`
package it imports.

## Configure

Set the server URL (and API key, if the host enabled auth) as environment
variables, then run the client:

**macOS / Linux:**

```bash
export SONDER_SERVER=https://sonder.example.com
export SONDER_API_KEY=s3cret
python3 sonder_client.py
```

**Windows (cmd):**

```bat
set SONDER_SERVER=https://sonder.example.com
set SONDER_API_KEY=s3cret
python sonder_client.py
```

Or use the `sonder-remote.cmd` wrapper if you have the repo checked out
locally (`sonder-remote.cmd` just calls `venv\Scripts\python.exe
sonder_client.py` with the same environment variables).

If the hosted server is unreachable, the client automatically retries the local
server at `SONDER_LOCAL_FALLBACK` (default `http://127.0.0.1:11435`) and
prints a warning before the reply. Set `SONDER_FALLBACK_LOCAL=0` to disable
that fallback. HTTP errors from the hosted server, such as bad API keys or
account bans, do not fall back.

If the server answers `421 HOST_NOT_ALLOWED`, it is an unauthenticated
`local-open` server that does not know the name in your URL. Use its IP
address, or add the name to `[server].allowed_hosts` / `SONDER_ALLOWED_HOSTS`
on the server. A server that requires an API key or accounts accepts any
name.

A reply that begins `refused /write:` (or another tool) was stopped by the
server's permission mode because nobody was at its console to answer the
prompt. The response's `sonder_receipt.refusal.call_id` names the call; a
developer or administrator can approve it once with
`POST /v1/approvals/<call_id>` and then resend the same request. See
[HTTP API & lifecycle](docs/wiki/05-http-api-and-lifecycle.md#one-shot-approvals-over-http).

`--server`/`--key` argv flags also work and override the env vars:

```bash
python3 sonder_client.py --server https://sonder.example.com --key s3cret
```

## Add a `sonder` command (macOS / Linux)

`sonder_client.py` has no shebang line and must run from its checkout, so
install a small wrapper that points at the clone instead of copying the file.
Run this from the repository directory so `$PWD` is the checkout path:

```bash
mkdir -p ~/.local/bin
printf '#!/bin/sh\nexec python3 "%s/sonder_client.py" "$@"\n' "$PWD" > ~/.local/bin/sonder
chmod +x ~/.local/bin/sonder
```

Make sure `~/.local/bin` is on your `PATH` (add `export
PATH="$HOME/.local/bin:$PATH"` to your shell rc if it isn't), then:

```bash
export SONDER_SERVER=https://sonder.example.com
export SONDER_API_KEY=s3cret
sonder
```

## Security note

Access to a hosted Sonder endpoint is equivalent to shell access to its allowed
workspaces. Anyone who has its API key and URL can invoke the configured tool
surface and consume host resources. Treat the key like a privileged password:

- Keep it out of shell history / dotfiles committed to git.
- Rotate it in `/etc/sonder/sonder.env` and restart `sonder` if it leaks.
- Never send the key over plaintext HTTP except to a loopback address. Remote
  clients must use HTTPS through the documented reverse proxy.
- Never expose or port-forward the runtime's loopback port. Restrict the TLS
  endpoint at the firewall or security-group layer as well.

## Behavior status

Labels follow the [documentation status vocabulary](docs/architecture/DOCUMENT-AUTHORITY-INDEX.md#documentation-status-vocabulary). Unfinished
implementation work is tracked only in the
[master implementation specification](docs/architecture/SONDER-MASTER-IMPLEMENTATION-SPEC.md).

| Behavior | Status | Boundary |
|---|---|---|
| Thin client run from a repository checkout | Implemented | Standard library plus the checkout's `sonder_runtime` client adapters; configured by `SONDER_SERVER`, `SONDER_API_KEY`, `--server`, and `--key`. |
| Copying `sonder_client.py` alone to another machine | Unsupported | The file imports `sonder_runtime` client adapters and fails with `ModuleNotFoundError` outside a checkout. |
| Automatic retry against the local server when the hosted server is unreachable | Implemented | `SONDER_FALLBACK_LOCAL=0` disables it; HTTP errors from the hosted server never fall back. |
| Direct execution without Python or a wrapper | Unsupported | `sonder_client.py` has no shebang line; invoke it with Python or the wrapper above. |
| Sending the API key over plaintext HTTP to a non-loopback host | Unsupported | Not a supported deployment; the client itself does not refuse it, so use HTTPS. |
| Resumable streams with sequence numbers and resume watermarks | Proposed | API-002; protocol-boundary validation exists, but the thin client does not resume streams. |
