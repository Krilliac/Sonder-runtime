# trilobite client — connect to a hosted server

`trilobite_client.py` is a **standalone** thin remote client: stdlib-only
Python, no repo checkout, no Ollama, no `mcp` package. Drop the one file on
any PC and point it at a trilobite instance hosted elsewhere (a VPS running
`trilobite_serve.py` as a systemd service — see the [hosting section of
deploy_trilobite.sh](deploy_trilobite.sh)).

## The three ways to use trilobite

1. **Hosted server + thin client (this doc).** Someone else (or your own
   VPS) runs the full learning loop; you talk to it over HTTP from any PC
   with just Python. No local GPU, no Ollama, no repo needed on the client
   side.
2. **Fully local.** Clone the repo and run the real loop on your own
   machine — the `trilobite` REPL / `trilobite.cmd` (Windows). See
   [README.md → Install / run](README.md#install--run).
3. **Integrated with Claude Code, via MCP.** `server.py` is registered as
   the `local-llm` MCP server; Claude Code calls `trilobite(...)`,
   `offload(...)`, etc. directly as tools. See [README.md →
   Interfaces](README.md#interfaces).

This doc covers #1.

## Requirements

Just **Python 3** (any recent 3.x). Nothing else — no repo clone, no
Ollama, no pip installs.

## Get the client

Grab the single file from the repo's raw GitHub URL:

```bash
curl -fsSL -o trilobite_client.py \
  https://raw.githubusercontent.com/Krilliac/Trilobite-model/main/trilobite_client.py
```

(Windows PowerShell equivalent: `curl.exe` ships with Windows 10/11 and
works the same way, or use `Invoke-WebRequest -Uri <url> -OutFile trilobite_client.py`.)

## Configure

Set the server URL (and API key, if the host enabled auth) as environment
variables, then run the client:

**macOS / Linux:**

```bash
export TRILOBITE_SERVER=http://your-vps:11435
export TRILOBITE_API_KEY=s3cret
python3 trilobite_client.py
```

**Windows (cmd):**

```bat
set TRILOBITE_SERVER=http://your-vps:11435
set TRILOBITE_API_KEY=s3cret
python trilobite_client.py
```

Or use the `trilobite-remote.cmd` wrapper if you have the repo checked out
locally (`trilobite-remote.cmd` just calls `venv\Scripts\python.exe
trilobite_client.py` with the same env vars).

If the hosted server is unreachable, the client automatically retries the local
server at `TRILOBITE_LOCAL_FALLBACK` (default `http://127.0.0.1:11435`) and
prints a warning before the reply. Set `TRILOBITE_FALLBACK_LOCAL=0` to disable
that fallback. HTTP errors from the hosted server, such as bad API keys or
account bans, do not fall back.

`--server`/`--key` argv flags also work and override the env vars:

```bash
python3 trilobite_client.py --server http://your-vps:11435 --key s3cret
```

## One-liner install (macOS / Linux) — get a `trilobite` command

```bash
mkdir -p ~/.local/bin
curl -fsSL -o ~/.local/bin/trilobite \
  https://raw.githubusercontent.com/Krilliac/Trilobite-model/main/trilobite_client.py
chmod +x ~/.local/bin/trilobite
```

`trilobite_client.py` has no shebang line, so add one (or invoke it via
`python3`) for direct execution:

```bash
sed -i '1i #!/usr/bin/env python3' ~/.local/bin/trilobite
```

Make sure `~/.local/bin` is on your `PATH` (add `export
PATH="$HOME/.local/bin:$PATH"` to your shell rc if it isn't), then:

```bash
export TRILOBITE_SERVER=http://your-vps:11435
export TRILOBITE_API_KEY=s3cret
trilobite
```

## Security note

**The API key is the only thing protecting a publicly hosted server.**
Anyone who has it (and the URL) can talk to your trilobite instance and
consume your VPS's compute. Treat it like a password:

- Keep it out of shell history / dotfiles committed to git.
- Rotate it (re-run `deploy_trilobite.sh --serve` with a fresh
  `TRILOBITE_API_KEY`, or edit `/etc/systemd/system/trilobite.service` and
  `systemctl daemon-reload && systemctl restart trilobite`) if it leaks.
- The proxy speaks plain HTTP by default — fine for casual/personal use,
  but for anything more, put it behind a reverse proxy (nginx/Caddy) with
  HTTPS (Let's Encrypt) so the key and traffic aren't sent in the clear,
  and consider restricting the port to specific source IPs at the
  firewall/security-group level instead of the whole internet.
