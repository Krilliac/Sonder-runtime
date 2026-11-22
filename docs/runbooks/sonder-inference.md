# Sonder Inference provider

Run the runtime's generation tiers on a local `sonder-infer serve` process
instead of Ollama, check it with doctor, and recover when it is down.
Preconditions: a `sonder-infer` build that has the `serve` command (Sonder
Inference, HTTP API version 1). This runbook is the runtime-side home for
connecting to it: on the same host (default), on another host behind a TLS
proxy ([Remote Inference](#remote-inference)), or as a private worker
([Private workers](#private-workers-a-second-pc)). Reference and the one home
of every `SONDER_INFERENCE_*` variable:
[sonder-inference-provider.md](../architecture/sonder-inference-provider.md).
Last checked against the code 2026-11-20.

## Start the server

Building, `sonder-infer serve` flags, token files, running it as a service
and the Caddy recipe live in the Inference repository's
[setup guide](https://github.com/Krilliac/Sonder-Inference/blob/main/docs/SETUP.md);
they are not repeated here. The runtime needs a server that answers
`GET /v1/sonder/health` (the gateway probes that path,
`adapters/inference/sonder_inference_gateway.py:1120`). The default endpoint is
`http://127.0.0.1:11437` (`DEFAULT_BASE_URL` in the same file), so a server
started with `sonder-infer serve --port 11437` needs no base URL setting. A
server started with `--ready-file` can instead be found through
`SONDER_INFERENCE_READY_FILE` (below).

Check it directly:

```bash
curl -s http://127.0.0.1:11437/v1/sonder/health
```

Expect `"status":"ready"` and `"api_version":1`. A `--backend mock` server
serves synthetic output; doctor reports it as a warning.

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
`--port 0`; the file format, size limit and the resolution order are in the
[variable table](../architecture/sonder-inference-provider.md). Restart the
runtime after changing bindings (bindings are fixed at startup).

Optional: `SONDER_INFERENCE_FALLBACK=ollama` sends requests Inference never
received (refused connection, unresolvable host, health not ready, 503
`not_ready`) to local Ollama once. It also serves chat steps Inference v1
cannot carry (decoder schemas, native tools, images) on loopback Ollama with
the tier's policy model, recorded as `served by ollama (ollama-only feature:
...)` in the turn receipt; without it those steps fail with a 400 naming this
setting. Leave it unset if a switch of model family is unacceptable for your
work.

## Verify

```bash
python -m sonder_runtime doctor
```

| Line | Meaning |
|---|---|
| `sonder_inference ok` | ready, API version 1 |
| `sonder_inference warn ... MOCK backend` | synthetic output; not for real work |
| `sonder_inference warn ... fall back to ollama` | down or not ready, fallback covers requests it never receives |
| `sonder_inference warn ... connection limit` | server up but at `--max-connections`; requests fail with `CapacityExceeded` until load drops |
| `sonder_inference fail ... start sonder-infer serve` | down with no fallback |
| `sonder_inference fail ... the fallback does not apply` | invalid `SONDER_INFERENCE_*` value, remote URL without consent, rejected key or Host, API version mismatch (health or ready file); fix the cause, a fallback cannot help |
| `sonder_inference_scope ok` | tier-bound agents, autopilot, fleet and helper calls resolve through Sonder Inference; explicit Ollama pins, strict `sonder` aliases and durable fanout remain Ollama-bound |

`--skip-inference` omits both lines. `python -m sonder_runtime preflight`
reports `sonder_inference` as a non-required check; `serve` never refuses to
start because Inference is down.

## Symptoms

- Chat through a bound tier fails with `DependencyUnavailable: Sonder
  Inference at http://127.0.0.1:11437 is not reachable or not ready
  (connection refused); start it with sonder-infer serve, or set
  SONDER_INFERENCE_FALLBACK=ollama and restart the runtime ...`.
- Runtime log WARNING `provider fallback sonder_inference -> ollama (count=N ...):
  Sonder Inference at ... is not reachable or not ready (connection refused)`
  when the fallback is configured. If Ollama then fails too, the error names
  both causes (`... (fallback to ollama after: ...)`); a tier mapped to a
  hosted Ollama model is refused with `Forbidden` on the fallback path.
- `CapacityExceeded: sonder-inference server is at its connection limit`
  under load; it clears by itself.
- `Forbidden: sonder-inference endpoint <scheme://host> is not loopback; remote
  inference requires ...` for a non-loopback base URL; the list names what is
  missing. See the failure table under Remote Inference.
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
  `127.0.0.1`, `localhost` or `[::1]` on a loopback-bound server. The runtime
  refuses other loopback aliases such as `127.0.0.2` at configuration.
- HTTP 503 `overloaded`: more concurrent connections than `--max-connections`.
- Proxy variables (`HTTP_PROXY`, `ALL_PROXY`, ...) do not apply to Inference
  traffic; `curl` honours them, so compare with `curl --noproxy '*'`.

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

Inference serves no TLS and, on a non-loopback `--host`, requires
`--token-file`. To reach one from another host, terminate TLS in a reverse
proxy in front of it (the Caddy recipe, including the required Host rewrite
and no response buffering, is in the Inference
[setup guide, "Behind Caddy"](https://github.com/Krilliac/Sonder-Inference/blob/main/docs/SETUP.md#behind-caddy)).
That link resolves once the Inference documentation change is merged. Then on
the runtime host:

1. Make the proxy's certificate trusted. The main endpoint is verified against
   the system trust store, merged with `SSL_CERT_FILE` when that is set (read
   at each new context build, `adapters/tls_contexts.py:56,83`;
   `adapters/inference/exchange_transport.py:132-136`). There is no
   per-endpoint CA for the main endpoint; only private workers carry a
   `ca_bundle`. Failure mode (2026-11-20): on Windows the OS store is merged
   in, and a stale certificate with the same subject as your private CA in the
   user store can make verification fail even though `SSL_CERT_FILE` trusts
   the right one (the same effect that `platform/config.py` documents for remote
   Ollama). Remove the stale certificate from the user store; doctor then shows
   `health probe failed: cannot reach endpoint` (see the failure table).
2. Set the variables (PowerShell shown; use `export` in bash):

   ```powershell
   $env:SONDER_MODEL_BACKEND = "sonder-inference"
   $env:SONDER_EMBEDDING_PROVIDER = "ollama"
   $env:SONDER_ALLOW_REMOTE_INFERENCE = "1"
   $env:SONDER_INFERENCE_BASE_URL = "https://sonder.example.com"
   $env:SONDER_INFERENCE_API_KEY = "<the server's --token-file contents>"
   ```

   - The base URL is the proxy's root, matching the Caddy recipe, which has
     no path prefix. Accepted forms, the path-prefix rule and the
     `SONDER_ALLOW_REMOTE_INFERENCE` values are in the
     [variable table](../architecture/sonder-inference-provider.md). A prefix
     such as `/sonder-inference` is sent unchanged to the proxy, so it works
     only if the proxy strips it (a Caddy `handle_path /sonder-inference/*`
     block that rewrites to the server); the recipe does not do this.
   - `SONDER_INFERENCE_API_KEY` is sent as the bearer token and must equal the
     token the server loads from its `--token-file`.
   - Prompt-bearing calls also need an operation context that allows cloud
     (`SONDER_ALLOW_CLOUD` enables it for the runtime's general request
     surfaces; some internal contexts always refuse it, for example A2A
     JSON-RPC, security evaluation and the provider-fallback path); others are refused
     with `Forbidden` before any byte is sent (`:936-946`). Health and
     identity probes do not need it.
3. Restart the runtime, then run `python -m sonder_runtime doctor`; expect
   `sonder_inference ok`. Checking by hand:
   in PowerShell
   `curl.exe -s -H "Authorization: Bearer $env:SONDER_INFERENCE_API_KEY" https://sonder.example.com/v1/sonder/health`
   (in bash, `$SONDER_INFERENCE_API_KEY`) should return `"status":"ready"`.

Environment proxy settings are ignored for Inference traffic and redirects are
never followed (`sonder_inference_gateway.py:256-277`), so a proxy that answers
with a 3xx is a failure, not a hop. Rollback: unset
`SONDER_INFERENCE_BASE_URL`, `SONDER_ALLOW_REMOTE_INFERENCE` and
`SONDER_INFERENCE_API_KEY` and restart.

| Symptom (exact text from code) | Cause and fix |
|---|---|
| `Forbidden: sonder-inference endpoint ... is not loopback; remote inference requires SONDER_ALLOW_REMOTE_INFERENCE=1, an https:// base URL, SONDER_INFERENCE_API_KEY` (only the missing items are listed) | Set the listed items (`:705-729`). |
| `Forbidden: sonder-inference endpoint ... is not loopback and this operation context does not allow prompts to leave the machine` | The caller's context does not allow cloud; enable it for that surface (`:941-946`). |
| `SONDER_ALLOW_REMOTE_INFERENCE must be 0 or 1, got ...` | Use `0`, `1` or leave it empty (`:679-681`). |
| `sonder-inference base URL must be http(s)://host[:port][/path] without credentials, query or fragment` | Remove userinfo, `?query` or `#fragment` (`:440-443`). |
| `sonder-inference rejected the credentials (HTTP 401 ...); check SONDER_INFERENCE_API_KEY` | Key differs from the server's token file (`:999-1002`). |
| `sonder-inference refused the request's Host (HTTP 403 forbidden_host); the base URL host must be 127.0.0.1, localhost or [::1]` | The proxy forwards its public Host to a loopback-bound server; rewrite Host in the proxy (`:993-998`). |
| Doctor detail `<endpoint>: health probe failed: cannot reach endpoint` (`:1125-1126`, `:1224`) | A TLS verification error (untrusted or stale certificate, see step 1) is reported this way, as is any other transport failure the gateway does not classify as a refused or unresolvable connection. The result is `fail` without `SONDER_INFERENCE_FALLBACK` and `warn` with it. Check `SSL_CERT_FILE` and the OS store, and compare with `curl`. |
| Doctor detail `<endpoint>: health returned HTTP ...` | Wrong path prefix or a proxy answering instead of Inference (`:1138-1176`). |

### Private workers (a second PC)

To spread requests over this host's server and another PC's, serve the other
PC's Inference behind its TLS proxy on the private link with `--token-file`,
keep the primary on loopback, and set:

```text
SONDER_ALLOW_REMOTE_INFERENCE=1
SONDER_INFERENCE_PRIVATE_WORKERS=[{"url":"https://10.0.0.2:8443","ca_bundle":"<sonder-home>/certs/worker1-ca.pem","token_env":"SONDER_INFERENCE_WORKER1_TOKEN","max_inflight":2}]
SONDER_INFERENCE_WORKER1_TOKEN=<the worker's token, from a secret store>
```

Restart the runtime. `provider_status()` (doctor, the ecosystem route) lists
each worker's health and in-flight count; the runtime log shows one
`sonder-inference placed request on ...` line per call. Both servers must
serve the requested model id, otherwise the worker is not chosen for it.

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
