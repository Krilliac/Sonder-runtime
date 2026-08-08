# Sonder Runtime thin client (`sonder_client.py`)

`sonder_client.py` is a **standalone** thin remote client: stdlib-only
Python, no repo checkout, no Ollama, no `mcp` package. Drop the one file on
any PC and point it at a Sonder Runtime host elsewhere. Remote hosts use the
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
   host; you talk to it over HTTPS from any PC with just Python. No local GPU,
   no Ollama, no repo needed on the client side.
2. **Fully local.** Clone the repo and run Sonder Runtime on your own
   machine — the `sonder` REPL / `sonder.cmd` (Windows). See
   [README.md → Install / run](README.md#install--run).
3. **Integrated with Claude Code, via MCP.** `server.py` is registered as
   the `sonder-runtime` MCP server; Claude Code calls `sonder(...)`,
   `offload(...)`, etc. directly as tools. See [README.md →
   Interfaces](README.md#interfaces).

This doc covers #1.

## Requirements

Just **Python 3** (any recent 3.x). Nothing else — no repo clone, no
Ollama, no pip installs.

## Get the client

Grab the single file from the repo's raw GitHub URL:

```bash
curl -fsSL -o sonder_client.py \
  https://raw.githubusercontent.com/Krilliac/Sonder-runtime/main/sonder_client.py
```

(Windows PowerShell equivalent: `curl.exe` ships with Windows 10/11 and
works the same way, or use `Invoke-WebRequest -Uri <url> -OutFile sonder_client.py`.)

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

`--server`/`--key` argv flags also work and override the env vars:

```bash
python3 sonder_client.py --server https://sonder.example.com --key s3cret
```

## One-liner install (macOS / Linux) — get a `sonder` command

```bash
mkdir -p ~/.local/bin
curl -fsSL -o ~/.local/bin/sonder \
  https://raw.githubusercontent.com/Krilliac/Sonder-runtime/main/sonder_client.py
chmod +x ~/.local/bin/sonder
```

`sonder_client.py` has no shebang line, so add one (or invoke it via
`python3`) for direct execution:

```bash
sed -i '1i #!/usr/bin/env python3' ~/.local/bin/sonder
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
