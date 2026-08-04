# Ollama outage

## Symptoms

- `/ready` returns 503 with `required dependency ollama is unavailable`.
- `/live` still returns 200 (the runtime process itself is healthy).
- Chat requests fail with 502/503/504 model errors.
- `sonder_model_calls_total{result="error"}` climbing.

The runtime is designed to report *unready without false success* during
an Ollama outage — do not restart Sonder to "fix" a red readiness check.

## Diagnosis

```bash
systemctl status ollama
curl -s http://127.0.0.1:11434/api/tags | head -c 200
journalctl -u ollama --since -15min
df -h /            # model pulls fill disks
free -h            # OOM-killed model runners
```

## Recovery

1. `systemctl restart ollama`
2. Watch Sonder recover on its own: the dependency probe runs every 15s;
   `/ready` flips back to 200 without a Sonder restart.
3. If Ollama flaps repeatedly, check GPU/driver state and disk, then hold
   Sonder in a drained state if needed:
   `python -m sonder_runtime drain --config /etc/sonder/sonder.toml`

## Aftermath

Check interrupted durable work (Autopilot/fleet) via `/v1/sonder/status`;
interrupted tasks stay explicit and require operator resume — they are
never silently replayed.
