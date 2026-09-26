# Sonder Inference provider

Run the runtime's generation tiers on a local `sonder-infer serve` process
instead of Ollama, check it with doctor, and recover when it is down.
Preconditions: a `sonder-infer` build that has the `serve` command (Sonder
Inference, HTTP API version 1) on the same host as the runtime. This runbook
assumes a workstation or the loopback server-private deployment; remote
Inference is covered in the last section. Reference:
[sonder-inference-provider.md](../architecture/sonder-inference-provider.md).

## Start the server

```bash
sonder-infer serve --backend ollama --model qwen2.5:7b --port 11437 \
    --ready-file "$HOME/.sonder/inference-ready.json"
```

The startup banner on stderr lists the listen URL, auth mode, models and
telemetry URLs. For wiring tests without weights, `--backend mock` serves the
model `mock`; the banner says `MOCK BACKEND - synthetic output, not a quality
or performance signal`, and doctor reports it as a warning.

Check it directly:

```bash
curl -s http://127.0.0.1:11437/v1/sonder/health
```

Expect `"status":"ready"` and `"api_version":1`.

## Bind tiers to it

Inference API v1 serves no embeddings, so keep embeddings on Ollama:

```text
SONDER_MODEL_BACKEND=sonder-inference
SONDER_EMBEDDING_PROVIDER=ollama
SONDER_INFERENCE_BASE_URL=http://127.0.0.1:11437
```

Or bind only some tiers, e.g. `SONDER_FAST_PROVIDER=sonder-inference`. Map
tiers to Inference model ids with `SONDER_INFERENCE_TIER_MODELS=fast=a,general=b`;
otherwise `SONDER_INFERENCE_MODEL` (default `default`, Inference's first
`--model`) is used. Instead of a base URL you may point
`SONDER_INFERENCE_READY_FILE` at the `--ready-file` path, which follows
`--port 0`. Restart the runtime after changing bindings.

Optional: `SONDER_INFERENCE_FALLBACK=ollama` sends requests Inference never
received (refused connection, unresolvable host, health not ready, 503
`not_ready`) to local Ollama once. Leave it unset if a silent switch of model
family is unacceptable for your work.

## Verify

```bash
python -m sonder_runtime doctor
```

| Line | Meaning |
|---|---|
| `sonder_inference ok` | ready, API version 1 |
| `sonder_inference warn ... MOCK backend` | synthetic output; not for real work |
| `sonder_inference warn ... fall back to ollama` | down, fallback covers requests it never receives |
| `sonder_inference fail` | down with no fallback, API version mismatch, or invalid bindings |
| `sonder_inference_scope warn` | always shown when bound: REPL, MCP, autopilot and fleet still generate through Ollama |

`--skip-inference` omits both lines. `python -m sonder_runtime preflight`
reports `sonder_inference` as a non-required check; `serve` never refuses to
start because Inference is down.

## Symptoms

- Chat through a bound tier fails with `DependencyUnavailable: Sonder
  Inference at http://127.0.0.1:11437 is not reachable or not ready (...);
  start it with sonder-infer serve, or set SONDER_INFERENCE_FALLBACK=ollama`.
- Runtime log WARNING `provider fallback sonder_inference -> ollama (count=N ...)`
  when the fallback is configured.
- `Forbidden: ... remote inference requires ...` for a non-loopback base URL.
- `incompatible sonder-inference API version` after upgrading one side only.

## Diagnosis

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:11437/v1/sonder/health
python -m sonder_runtime doctor --json | python -m json.tool
env | grep -E '^SONDER_(MODEL_BACKEND|.*_PROVIDER|INFERENCE_|ALLOW_REMOTE_INFERENCE)'
```

- Connection refused: the server is not running or listens on another port
  (compare with the ready file's `url`).
- HTTP 503 with `"status":"starting"`: the model is still loading; the runtime
  treats this as not ready and re-probes after `SONDER_INFERENCE_HEALTH_TTL_SECONDS`
  (default 5).
- HTTP 401: the server has `--token-file`; set `SONDER_INFERENCE_API_KEY`.
- HTTP 403 `forbidden_host`: the base URL names a host other than
  `127.0.0.1`, `localhost` or `[::1]` on a loopback-bound server.

## Recovery

1. Start or restart `sonder-infer serve` (above). A restart gives it a new
   instance id; the runtime's health cache refreshes within the TTL, with no
   runtime restart.
2. If the server cannot come back soon, either set
   `SONDER_INFERENCE_FALLBACK=ollama` and restart the runtime, or rebind the
   affected tiers to `ollama`.
3. On an API version mismatch, upgrade the older side; the runtime refuses
   any major version other than 1 and never falls back for it.

## Remote Inference

Inference serves no TLS. Put it behind a TLS-terminating proxy, start it with
`--token-file`, and set all of `SONDER_ALLOW_REMOTE_INFERENCE=1`, an
`https://` `SONDER_INFERENCE_BASE_URL`, and `SONDER_INFERENCE_API_KEY`. Only
operation contexts that allow cloud may send prompts there; others are
refused with `Forbidden` before any byte is sent.

## Attestation

```bash
python scripts/backend_attest.py --backend sonder-inference --dry-run
python scripts/backend_attest.py --backend sonder-inference --protocol-probes
```

The identity comes from the server's `/v1/sonder/identity`. A mock (synthetic)
identity is never recorded; `--protocol-probes` refuses it.

## Aftermath

- `doctor` shows `sonder_inference ok`.
- If the fallback fired, check the WARNING count in the log and
  `fallback_count` in provider status; answers served by Ollama came from a
  different model.
