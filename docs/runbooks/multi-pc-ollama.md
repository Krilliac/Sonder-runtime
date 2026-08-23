# Multi-PC Ollama inference

Sonder can distribute independent inference requests across multiple Ollama
hosts. This is request-level pooling: every host has its own model files and
memory, and Sonder does not shard one model across machines.

## Topology

```text
PC 1: Sonder coordinator ── HTTPS ──> PC 2: TLS proxy ── loopback ──> Ollama
       local Ollama is worker 1       PC 3 may be added the same way
```

The coordinator uses least-inflight scheduling. A worker is circuit-broken
after three transport failures, and a request fails over only before a worker
returns a response. A model error or a completed response is never replayed on
another PC.

## Prepare each worker PC

Install Ollama and pull the exact model aliases used by the coordinator:

```powershell
ollama pull sonder:latest
ollama pull qwen3-coder:30b-a3b-q4_K_M
```

Keep Ollama bound to loopback and put a TLS reverse proxy in front of it. The
proxy certificate must be trusted by the coordinator; use a private CA for a
direct Ethernet/Wi-Fi network or place both PCs behind a VPN. Do not expose
plain Ollama HTTP directly to the LAN or Internet.

For example, with Caddy on the worker PC:

```text
ollama.example.internal {
    reverse_proxy 127.0.0.1:11434
}
```

Use a hostname that resolves across the private link and verify from the
coordinator that `https://ollama.example.internal/api/version` is reachable.

## Configure the coordinator PC

Leave the coordinator's own Ollama as the primary endpoint and add remote
workers in its secrets environment or process environment:

```powershell
$env:OLLAMA_HOST = "http://127.0.0.1:11434"
$env:SONDER_ALLOW_REMOTE_OLLAMA = "1"
$env:SONDER_OLLAMA_WORKERS = "https://ollama-pc2.example.internal:443;https://ollama-pc3.example.internal:443"
python -m sonder_runtime preflight
python -m sonder_runtime serve
```

The equivalent TOML configuration is:

```toml
[ollama]
url = "http://127.0.0.1:11434"
allow_remote = true
workers = [
  "https://ollama-pc2.example.internal:443",
  "https://ollama-pc3.example.internal:443",
]
```

Every remote worker must use HTTPS, have no credentials embedded in its URL,
and have the matching model tag installed. If the consent gate, URL, or TLS
requirements are wrong, startup fails closed rather than silently routing
prompts over an insecure link.

## Verify

Use the normal status surface. It reports the configured worker count and how
many workers are remote. Send several independent requests and inspect the
activity/model telemetry to see the selected backend. A worker that goes down
is temporarily removed from selection and returns automatically after its
cooldown when a later request succeeds.

## Internet access

For Internet use, put the worker endpoint behind a VPN or an authenticated TLS
reverse proxy with firewall allow-listing. Never port-forward Ollama's raw
`11434` port or Sonder's loopback service. The coordinator's API authentication
and the worker's TLS boundary are separate controls.
