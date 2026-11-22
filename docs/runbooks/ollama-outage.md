# Ollama outage

## Symptoms

- `/ready` returns 503 with `required dependency ollama is unavailable`.
- `/live` still returns 200 (the runtime process itself is healthy).
- Chat requests fail with 502/503/504 model errors.
- `sonder_model_calls_total{result="error"}` climbing.

The runtime is designed to report *unready without false success* during
an Ollama outage — do not restart Sonder to "fix" a red readiness check.

## Diagnosis (local Ollama)

```bash
systemctl status ollama
curl -s http://127.0.0.1:11434/api/tags | head -c 200
journalctl -u ollama --since -15min
df -h /            # model pulls fill disks
free -h            # OOM-killed model runners
```

## Remote primary or worker

Use this branch when `OLLAMA_HOST` points at another machine, or when pooled
workers are configured (`[ollama].workers`, see
[multi-pc-ollama.md](multi-pc-ollama.md)). Checked against code 2026-11-20.

1. Worker down (pool only): after `worker_failure_threshold` consecutive
   failures, including 502, 503 and 504 (3 by default) the worker's circuit opens for
   `worker_cooldown_seconds` (30 s by default). Further consecutive failures
   lengthen the cooldown, up to eight times the base, and traffic goes to the
   other workers. A recovered worker is retried with one trial request at a
   time. Circuit numbers and the full failover rules:
   [multi-pc-ollama.md](multi-pc-ollama.md) (`sonder_runtime/adapters/inference/ollama_pool.py:62-63`,
   `:1234-1236`).
2. Proxy (Caddy) returns 502, 503 or 504 because it cannot reach Ollama. This
   step applies only when `[ollama].workers` is configured: with a remote
   `OLLAMA_HOST` and no workers the pool is disabled and every request goes to
   that one primary (`server.py:5553-5554`), so there is nothing to fail over
   to and the error surfaces. With workers, an ordinary chat request fails over
   to another worker and counts toward the circuit, as do transport errors that
   happen before any response. `/api/generate` load, unload and prewarm calls
   are not idempotent and never fail over, nor do `single_send` calls or calls
   pinned to the primary. Any other reply, including 500, 501, 404 for a
   missing model and 403 from a proxy allowlist, is not failed over (only 502,
   503 and 504 are), so it is a configuration fix, not an outage. The full
   rules and line references are in [multi-pc-ollama.md](multi-pc-ollama.md).
3. Look at worker health without sending inference traffic:
   `python -m sonder_runtime doctor`. The `ollama_workers` check and its
   `ok`, `warn`, `fail` and `skipped` results are described in
   [multi-pc-ollama.md](multi-pc-ollama.md#verify).
4. See the circuit and error categories: the `ollama_pool_admin_status` MCP
   tool or `GET /v1/sonder/ollama-pool` (`sonder_runtime/interfaces/http/serve.py:5571-5572`;
   administrator only in authenticated deployments).
5. Check the proxy from the coordinator, not from the worker, with the same
   CA file you gave Sonder in `[ollama].ca_bundle` (omit `--cacert` for a
   publicly trusted certificate; use `curl.exe` in PowerShell):
   `curl --cacert <ca-file> https://<worker-host>:<proxy-port>/api/version`.
   A TLS error is a trust problem, a timeout is the network or firewall, a 502
   means Ollama behind the proxy is down. Details:
   [remote-ollama-caddy.md](remote-ollama-caddy.md#verify) and
   [Troubleshooting](remote-ollama-caddy.md#troubleshooting).
6. Then restart Ollama on the worker. On Linux use `systemctl restart ollama`
   (Recovery below). On a Windows worker run by the scheduled task from
   [multi-node-ollama.md](multi-node-ollama.md#windows-recommended-for-windows-worker-nodes),
   end the `ollama.exe` process: the task's loop restarts it after its delay,
   or restart the scheduled task itself.

`/ready` probes the primary through the same transport as chat calls
(`ollama_endpoint.open_url` on `<[ollama].url>/api/tags`,
`sonder_runtime/adapters/web/lifecycle.py` `probe_ollama_once`), so it uses
`[ollama].ca_bundle` (`SONDER_OLLAMA_CA_BUNDLE`) and ignores environment
proxies like chat does; a private-CA proxy that chat reaches does not by itself
make `/ready` report `required dependency ollama is unavailable`.
Workers in `[ollama].workers` are not probed by `/ready`.

## Recovery

1. `systemctl restart ollama`
2. Watch Sonder recover on its own: the dependency probe runs every 15s;
   `/ready` flips back to 200 without a Sonder restart.
3. If Ollama flaps repeatedly, check GPU/driver state and disk, then drain
   and stop Sonder if needed (drain waits for in-flight work up to the drain
   deadline; work still running at the deadline is left `interrupted`; the server
   then exits with code 0, so systemd's `Restart=on-failure` does not restart it):
   `python -m sonder_runtime drain --config /etc/sonder/sonder.toml --secrets /etc/sonder/sonder.env`

## Aftermath

Check interrupted durable work (Autopilot/fleet) via `/v1/sonder/status`;
interrupted tasks stay explicit and require operator resume — they are
never silently replayed.
